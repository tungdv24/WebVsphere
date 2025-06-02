from flask import Flask, render_template, request, redirect, url_for, session
from pyVim import connect
from pyVmomi import vim, vmodl
from functools import wraps
from collections import defaultdict
from flask import jsonify
import ssl
from clone import clone_bp
from vsphere_session import connect_to_vcenter, save_session
import sqlite3
from log import log_system_action, log_error
import json
from datetime import datetime, timedelta
from flask import Response, stream_with_context
import time, json
from pyVim.task import WaitForTask
from flask import redirect, url_for
from urllib.parse import quote
from zabbix import delete_host_from_zabbix
from pyVim.task import WaitForTask
from clone_v2 import clone_v2_bp

app = Flask(__name__)
app.secret_key = '4pKKSm2uNBFBNOFPcXfPuRaeStrLnBtB'
context = ssl._create_unverified_context()
app.register_blueprint(clone_bp, url_prefix='/clone')
app.register_blueprint(clone_v2_bp, url_prefix='/clone_v2')

def wait_for_task(task):
    """Waits for a vSphere task to complete."""
    WaitForTask(task)  # pyVmomi built-in wait


# --------------------- Decorator ---------------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --------------------- vSphere Utilities ---------------------
def get_vm_by_moid(si, moid):
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )

    for vm in container.view:
        if vm._moId != moid:
            continue

        # Datastores
        datastore_names = [ds.name for ds in vm.datastore] if vm.datastore else ['N/A']

        # Networks
        network_names = []
        if vm.config and vm.config.hardware.device:
            for device in vm.config.hardware.device:
                if isinstance(device, vim.vm.device.VirtualEthernetCard):
                    if hasattr(device.backing, 'network') and device.backing.network:
                        network_names.append(device.backing.network.name)
                    elif hasattr(device.backing, 'deviceName'):
                        network_names.append(device.backing.deviceName)
        if not network_names:
            network_names = ['N/A']

        # Disk Size Calculation (in GB)
        disk_size_kb = sum(
            device.capacityInKB for device in vm.config.hardware.device
            if isinstance(device, vim.vm.device.VirtualDisk)
        ) if vm.config and vm.config.hardware.device else 0
        disk_size_gb = round(disk_size_kb / (1024 * 1024), 2)  # GB, rounded

        # Resource Pool
        resource_pool_name = vm.resourcePool.name if vm.resourcePool else 'N/A'

        # Cluster
        cluster_name = 'N/A'
        try:
            host = vm.runtime.host
            if hasattr(host, 'parent') and hasattr(host.parent, 'name'):
                cluster_name = host.parent.name
        except Exception:
            pass

        # Final dictionary return
        return {
            'moid': vm._moId,
            'name': vm.name,
            'power_state': vm.runtime.powerState,
            'guest_os': vm.config.guestFullName if vm.config else 'N/A',
            'ip_address': vm.summary.guest.ipAddress if vm.summary.guest else 'N/A',
            'num_cpu': vm.config.hardware.numCPU if vm.config else 'N/A',
            'memory_mb': vm.config.hardware.memoryMB if vm.config else 'N/A',
            'disk_size_gb': disk_size_gb,
            'annotation': vm.config.annotation if vm.config and vm.config.annotation else '',
            'host': vm.runtime.host.name if vm.runtime.host else 'N/A',
            'datastores': datastore_names,
            'networks': network_names,
            'resource_pool': resource_pool_name,
            'cluster': cluster_name
        }

    return None
def get_vm_by_moid_object(si, moid):
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)

    for vm in container.view:
        if vm._moId == moid:
            return vm

    return None

def get_vm_usage_data(vm):
    summary = vm.summary
    stats = {
        "cpu": summary.quickStats.overallCpuUsage or 0,       # in MHz
        "ram": summary.quickStats.guestMemoryUsage or 0,      # in MB
        "net": summary.quickStats.overallNetworkUsage or 0,   # might be None or custom to compute
    }
    return stats

# --------------------- Routes ---------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        vcenter_host = request.form['vcenter_host']
        username = request.form['username']
        password = request.form['password']

        try:
            si = connect_to_vcenter(vcenter_host, username, password)

            if not si:
                raise Exception("Failed to establish connection to vCenter.")

            session_id = f"{username}@{vcenter_host}"
            save_session(session_id, si)

            session['logged_in'] = True
            session['vcenter_host'] = vcenter_host
            session['username'] = username
            session['session_id'] = session_id
            session['password'] = password

            return redirect(url_for('index'))

        except vim.fault.InvalidLogin:
            error = "Invalid login credentials."
            log_error(error, context=f"vCenter: {vcenter_host}, Username: {username}")
        except Exception as e:
            error = f"Failed to connect to vCenter: {e}"
            log_error(str(e), context=f"vCenter: {vcenter_host}, Username: {username}")

        return render_template('login.html', error=error)

    return render_template('login.html', error=error)

@app.template_filter('urlencode')
def urlencode_filter(s):
    return quote(s)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    vcenter_host = session.get('vcenter_host')
    username = session.get('username')
    password = session.get('password')
    vms_by_cluster_pool = defaultdict(lambda: defaultdict(list))  # cluster -> pool -> VMs
    si = None

    try:
        si = connect_to_vcenter(vcenter_host, username, password)
        content = si.RetrieveContent()
        datacenters = content.rootFolder.childEntity

        for dc in datacenters:
            if hasattr(dc, 'hostFolder'):
                def recurse_folder(folder):
                    for entity in folder.childEntity:
                        if isinstance(entity, (vim.ClusterComputeResource, vim.ComputeResource)):
                            cluster_name = entity.name
                            for host in entity.host:
                                for vm in host.vm:
                                    pool_name = vm.resourcePool.name if vm.resourcePool else 'No Resource Pool'
                                    vms_by_cluster_pool[cluster_name][pool_name].append({
                                        'name': vm.name,
                                        'moid': vm._moId,
                                        'power_state': vm.runtime.powerState
                                    })
                        elif isinstance(entity, vim.Folder):
                            recurse_folder(entity)
                recurse_folder(dc.hostFolder)

    except Exception as e:
        print(f"❌ Index error: {e}")

    finally:
        if si:
            connect.Disconnect(si)

    return render_template('index.html', vms_by_cluster_pool=vms_by_cluster_pool)

@app.route('/vm/<moid>')
@login_required
def get_vm_details(moid):
    try:
        si = connect_to_vcenter(session['vcenter_host'], session['username'], session['password'])
        vm = get_vm_by_moid(si, moid)

        if not vm:
            log_error("VM not found", context=f"MOID: {moid}, User: {session.get('username')}")
            return '<p>⚠️ Virtual machine not found.</p>', 404
        
        embeded_name = quote(vm["name"])
        return render_template('vm_partial.html', vm=vm, embeded_name=embeded_name)

    except Exception as e:
        error_msg = f"Error loading VM details: {e}"
        log_error(error_msg, context=f"MOID: {moid}, User: {session.get('username')}")
        print(f"❌ {error_msg}")
        return '<p>❌ Error loading virtual machine details.</p>', 500

from pyVim.task import WaitForTask  # Make sure this import exists

def wait_for_task(task):
    return WaitForTask(task)

@app.route('/action/<moid>', methods=['POST'])
@login_required
def vm_action(moid):
    action = request.form.get('action')
    si = connect_to_vcenter(session['vcenter_host'], session['username'], session['password'])
    vm_obj = get_vm_by_moid_object(si, moid)

    try:
        if not vm_obj:
            msg = "Virtual machine not found"
            log_error(msg, context=f"MOID: {moid}, User: {session.get('username')}")
            log_system_action(session['username'], session['vcenter_host'], moid, action, "Failed - VM not found")
            if action == 'delete':
                return jsonify({"error": msg})
            return "<p>⚠️ Virtual machine not found.</p>"

        if action == 'start' and vm_obj.runtime.powerState != 'poweredOn':
            task = vm_obj.PowerOn()
            wait_for_task(task)

        elif action == 'stop' and vm_obj.runtime.powerState != 'poweredOff':
            task = vm_obj.PowerOff()
            wait_for_task(task)

        elif action == 'reboot' and vm_obj.runtime.powerState == 'poweredOn':
            try:
                vm_obj.RebootGuest()  # Doesn't return a task
            except Exception as reboot_err:
                raise Exception(f"Guest reboot failed: {reboot_err}")

        elif action == 'snapshot':
            user = session.get('username', 'unknown_user')
            snap_name_input = request.form.get('snap_name') or "Snapshot"
            date_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            snap_name = f"{snap_name_input}_{user}_{date_str}"
            description = f"Snapshot taken by {user} at {date_str.replace('_', ' ')}"

            task = vm_obj.CreateSnapshot_Task(
                name=snap_name,
                description=description,
                memory=False,
                quiesce=False
            )
            wait_for_task(task)

        elif action == 'delete':
            if vm_obj.runtime.powerState != 'poweredOff':
                msg = "❌ Cannot delete VM that is not powered off."
                log_system_action(session['username'], session['vcenter_host'], vm_obj.name, action, "Failed - VM not powered off")
                return jsonify({"error": msg})

            vm_name = vm_obj.name  # Save before deletion
            try:
                delete_host_from_zabbix(vm_name)
            except Exception as zbx_err:
                log_error(f"Zabbix deletion failed: {zbx_err}", context=f"VM: {vm_name}")

            destroy_task = vm_obj.Destroy_Task()
            wait_for_task(destroy_task)
            log_system_action(session['username'], session['vcenter_host'], vm_name, action, "Success")
            connect.Disconnect(si)
            return jsonify({"redirect": True})

        # For non-delete actions: update and render partial HTML
        log_system_action(session['username'], session['vcenter_host'], vm_obj.name, action, "Success")
        updated_vm = get_vm_by_moid(si, moid)
        return render_template('vm_partial.html', vm=updated_vm)

    except Exception as e:
        error_msg = f"Error performing action '{action}': {e}"
        log_error(error_msg, context=f"User: {session.get('username')}, VM: {moid}")
        log_system_action(session['username'], session['vcenter_host'], vm_obj.name if vm_obj else moid, action, f"Failed - {str(e)}")

        if action == 'delete':
            return jsonify({"error": str(e)})

        return f"<p>❌ Error performing action: {e}</p>"

    finally:
        connect.Disconnect(si)



@app.route('/snapshots/<moid>')
@login_required
def list_snapshots(moid):
    try:
        si = connect_to_vcenter(session['vcenter_host'], session['username'], session['password'])
        vm = get_vm_by_moid_object(si, moid)
        snapshot_list = []

        def collect_snapshots(tree):
            if tree is None:
                return
            snapshot_list.append({
                "name": tree.name,
                "description": tree.description,
                "created": tree.createTime.strftime("%Y-%m-%d %H:%M:%S"),
                "moid": tree.snapshot._moId
            })
            for child in tree.childSnapshotList:
                collect_snapshots(child)

        if vm.snapshot:
            for root in vm.snapshot.rootSnapshotList:
                collect_snapshots(root)

        return jsonify(snapshot_list)

    except Exception as e:
        print("❌ Error fetching snapshots:", e)
        return jsonify({"error": str(e)}), 500

@app.route('/revert_snapshot/<moid>', methods=['POST'])
@login_required
def revert_snapshot(moid):
    snapshot_moid = request.form.get('snapshot_moid')
    try:
        si = connect_to_vcenter(session['vcenter_host'], session['username'], session['password'])
        vm = get_vm_by_moid_object(si, moid)

        snap = find_snapshot_by_moid(vm.snapshot.rootSnapshotList, snapshot_moid)
        if snap:
            snap.snapshot.RevertToSnapshot_Task()
            return jsonify({"message": "Reverted successfully!"})
        else:
            return jsonify({"error": "Snapshot not found"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/delete_snapshot/<moid>', methods=['POST'])
@login_required
def delete_snapshot(moid):
    snapshot_moid = request.form.get('snapshot_moid')
    try:
        si = connect_to_vcenter(session['vcenter_host'], session['username'], session['password'])
        vm = get_vm_by_moid_object(si, moid)

        snap = find_snapshot_by_moid(vm.snapshot.rootSnapshotList, snapshot_moid)
        if snap:
            snap.snapshot.RemoveSnapshot_Task(removeChildren=False)
            return jsonify({"message": "Deleted successfully!"})
        else:
            return jsonify({"error": "Snapshot not found"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500

def find_snapshot_by_moid(tree_list, target_moid):
    for node in tree_list:
        if node.snapshot._moId == target_moid:
            return node
        result = find_snapshot_by_moid(node.childSnapshotList, target_moid)
        if result:
            return result
    return None

@app.route('/logs')
def view_logs():
    try:
        with open("deployment_log.json", "r") as f:
            deploy_logs = json.load(f)
            deploy_logs.sort(key=lambda log: datetime.strptime(log["timestamp"], "%Y-%m-%d %H:%M:%S"), reverse=True)
    except:
        deploy_logs = []

    try:
        with open("system_logs.json", "r") as f:
            system_logs = json.load(f)
            system_logs.sort(key=lambda log: datetime.strptime(log["timestamp"], "%Y-%m-%d %H:%M:%S"), reverse=True)
    except:
        system_logs = []

    try:
        with open("error.json", "r") as f:
            recent_errors = json.load(f)
            recent_errors.sort(key=lambda log: datetime.strptime(log["timestamp"], "%Y-%m-%d %H:%M:%S"), reverse=True)
    except:
        recent_errors = []

    return render_template(
        "logs.html",
        logs=deploy_logs,
        system_logs=system_logs,
        recent_errors=recent_errors
    )
# --------------------- Run ---------------------
if __name__ == '__main__':
    app.run(debug=True)
