from flask import Blueprint, render_template, jsonify, request, session
import os
import json
import sqlite3
from pyVmomi import vim
from pyVim.connect import Disconnect
from vsphere_session import connect_vsphere_from_session, login_required
from deploy import deploy_vms_from_config_stream
from log import log_system_action, log_error
import traceback
from flask import Response, stream_with_context

clone_bp = Blueprint('clone', __name__)

# ------------------- Database Helpers ------------------- #

DB_PATH = 'vm_data.db'

def get_user_host():
    user = session.get('username', 'default_user')
    host = session.get('vcenter_host', 'default_host')
    return f"{user}_{host}"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def save_vm_config_to_db(user_host, config_list):
    db = get_db()
    cursor = db.cursor()
    config_json = json.dumps(config_list, indent=4)
    cursor.execute('''
        INSERT INTO vm_data (user_host, config_json, spec_json)
        VALUES (?, ?, ?)
        ON CONFLICT(user_host) DO UPDATE SET config_json=excluded.config_json
    ''', (user_host, config_json, '[]'))
    db.commit()
    db.close()

def get_vm_config_from_db(user_host):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT config_json FROM vm_data WHERE user_host = ?', (user_host,))
    row = cursor.fetchone()
    db.close()
    return json.loads(row['config_json']) if row else []

def save_vm_spec_to_db(user_host, spec_list):
    db = get_db()
    cursor = db.cursor()
    spec_json = json.dumps(spec_list, indent=4)
    cursor.execute('UPDATE vm_data SET spec_json = ? WHERE user_host = ?', (spec_json, user_host))
    db.commit()
    db.close()

def get_vm_spec_from_db(user_host):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT spec_json FROM vm_data WHERE user_host = ?', (user_host,))
    row = cursor.fetchone()
    db.close()
    return json.loads(row['spec_json']) if row else []

# ------------------- vSphere Data Fetch ------------------- #

def fetch_host_resources_and_pools(service_instance):
    content = service_instance.RetrieveContent()
    host_data = []
    resource_pools = []

    for datacenter in content.rootFolder.childEntity:
        if hasattr(datacenter, 'hostFolder'):
            clusters = datacenter.hostFolder.childEntity
            for cluster in clusters:
                if isinstance(cluster, vim.ClusterComputeResource):
                    # Host info
                    for host in cluster.host:
                        summary = host.summary
                        hardware = summary.hardware
                        quick_stats = summary.quickStats

                        total_cpu_ghz = hardware.cpuMhz * hardware.numCpuCores / 1000
                        used_cpu_ghz = quick_stats.overallCpuUsage / 1000 if quick_stats.overallCpuUsage else 0
                        total_ram_gb = hardware.memorySize / (1024 ** 3)
                        used_ram_gb = quick_stats.overallMemoryUsage / 1024 if quick_stats.overallMemoryUsage else 0
                        free_disk_gb = sum(ds.summary.freeSpace for ds in host.datastore) / (1024 ** 3)

                        host_info = {
                            "Host": summary.config.name,
                            "Cluster": cluster.name,
                            "Available CPU (GHz)": round(total_cpu_ghz - used_cpu_ghz, 1),
                            "Available RAM (GB)": round(total_ram_gb - used_ram_gb, 1),
                            "Available Disk (GB)": round(free_disk_gb, 1)
                        }
                        host_data.append(host_info)

                    # Resource Pools
                    def collect_pool_data(pool, cluster_name):
                        try:
                            cpu_usage = pool.runtime.cpu
                            mem_usage = pool.runtime.memory

                            cpu_available = (cpu_usage.maxUsage - cpu_usage.overallUsage) / 1000
                            ram_available = (mem_usage.maxUsage - mem_usage.overallUsage) / 1024 / 1024 / 1024

                            resource_pools.append({
                                "Cluster": cluster_name,
                                "Resource Pool": pool.name,
                                "Available CPU (GHz)": round(cpu_available, 2),
                                "Available RAM (GB)": round(ram_available, 2)
                            })

                            for sub_pool in getattr(pool, 'resourcePool', []):
                                collect_pool_data(sub_pool, cluster_name)

                        except Exception as e:
                            print(f"⚠️ Skipping resource pool '{getattr(pool, 'name', 'Unnamed')}' in {cluster_name}: {e}")

                    try:
                        collect_pool_data(cluster.resourcePool, cluster.name)
                    except Exception as e:
                        print(f"⚠️ Skipping all resource pools in cluster '{cluster.name}': {e}")

    return host_data, resource_pools



def process_resource_pool(pool, cluster_name):
    pool_data = []

    def extract_pool_info(rp):
        cpu_mhz = rp.runtime.cpu.allocationLimit if rp.runtime.cpu else None
        ram_mb = rp.runtime.memory.allocationLimit if rp.runtime.memory else None

        return {
            "Cluster": cluster_name,
            "Resource Pool": rp.name,
            "CPU Limit (MHz)": cpu_mhz if cpu_mhz != -1 else "Unlimited",
            "RAM Limit (MB)": ram_mb if ram_mb != -1 else "Unlimited"
            # Disk info isn't directly tracked per pool in vSphere
        }

    pool_data.append(extract_pool_info(pool))

    for sub_pool in pool.resourcePool:
        pool_data.append(extract_pool_info(sub_pool))

    return pool_data

# ------------------- Routes ------------------- #

@clone_bp.route('/', methods=['GET'])
@login_required
def clone():
    host = session.get('vcenter_host')
    username = session.get('username')
    password = session.get('password')

    if not all([host, username, password]):
        return "Missing credentials in session", 401
    return render_template('clone_dashboard.html', username=username, host=host)

@clone_bp.route('/get_host_resources', methods=['GET'])
@login_required
def get_host_resources():
    si = connect_vsphere_from_session(session)
    if si:
        try:
            host_data, resource_pools = fetch_host_resources_and_pools(si)
            return jsonify({
                "hosts": host_data,
                "pools": resource_pools
            })
        except Exception as e:
            return jsonify({"error": f"An error occurred: {str(e)}"}), 500
    return jsonify({"error": "Failed to connect to vSphere"}), 500


@clone_bp.route('/get_configured_vms', methods=['GET'])
@login_required
def get_configured_vms():
    try:
        user_host = get_user_host()
        return jsonify(get_vm_config_from_db(user_host))
    except:
        return jsonify([])

@clone_bp.route('/get_finalized_vms', methods=['GET'])
@login_required
def get_finalized_vms():
    try:
        user_host = get_user_host()
        return jsonify(get_vm_spec_from_db(user_host))
    except:
        return jsonify([])

@clone_bp.route('/submit', methods=['POST'])
@login_required
def submit():
    data = {
        "NameVM": request.form['NameVM'],
        "SourceVM": request.form['SourceVM'],
        "Count": int(request.form['Count']),
        "CPU": int(request.form['CPU']),
        "RAM": int(request.form['RAM']),
        "Disk": int(request.form['Disk']),
        "Datastore": request.form['Datastore'],
        "Network": request.form['Network'],
        "IPList": request.form['IPList'].split(','),
        "DNS": request.form['DNS'].split(','),
        "SubnetMask": request.form['SubnetMask'],
        "Gateway": request.form['Gateway']
    }

    try:
        user_host = get_user_host()
        existing_configs = get_vm_config_from_db(user_host)
        existing_configs.append(data)
        save_vm_config_to_db(user_host, existing_configs)
        return jsonify({"message": "VM configuration added successfully"})
    except Exception as e:
        return jsonify({"message": "Failed to save configuration", "error": str(e)}), 500

@clone_bp.route('/remove_vm/<int:index>', methods=['POST'])
@login_required
def remove_vm(index):
    try:
        user_host = get_user_host()
        vm_configs = get_vm_config_from_db(user_host)
        if 0 <= index < len(vm_configs):
            vm_configs.pop(index)
            save_vm_config_to_db(user_host, vm_configs)
            return jsonify({"message": "VM removed successfully"})
        return jsonify({"message": "Invalid index"}), 400
    except Exception as e:
        return jsonify({"message": "An error occurred", "error": str(e)}), 500

@clone_bp.route('/clear_all', methods=['POST'])
@login_required
def clear_all():
    try:
        user_host = get_user_host()
        save_vm_config_to_db(user_host, [])
        save_vm_spec_to_db(user_host, [])
        return jsonify({"message": "All VM configurations cleared successfully"})
    except Exception as e:
        return jsonify({"message": "An error occurred", "error": str(e)}), 500

@clone_bp.route('/finalize', methods=['POST'])
@login_required
def finalize():
    try:
        user_host = get_user_host()
        generate_vm_spec_db(user_host)
        return jsonify({"message": "VM specifications finalized and saved."})
    except Exception as e:
        return jsonify({"message": "An error occurred", "error": str(e)}), 500

@clone_bp.route('/create', methods=['GET'])
@login_required
def create():
    try:
        session_id = session.get('session_id')
        print(f"🔐 Session ID: {session_id}")  # Debug

        def event_stream():
            for message in deploy_vms_from_config_stream(session_id):
                yield message

        # Ensure streaming context is preserved
        return Response(stream_with_context(event_stream()), mimetype='text/event-stream')

    except Exception as e:
        error_message = f"🔥 Unexpected error in create(): {str(e)}"
        print(error_message)
        traceback.print_exc()
        log_error(str(e), context="create() route - exception")
        return jsonify({"message": "Unexpected error", "error": str(e)}), 500




# ------------------- VM Spec Generator ------------------- #

def generate_vm_spec_db(user_host):
    vm_configs = get_vm_config_from_db(user_host)
    new_vm_specs = []

    for vm_config in vm_configs:
        base_name = vm_config["NameVM"]
        ip_list = vm_config["IPList"]
        count = vm_config["Count"]

        for i in range(count):
            name = base_name if count == 1 else f"{base_name}-{i+1}"
            ip = ip_list[i] if i < len(ip_list) else ""
            new_vm = {
                "NameVM": name,
                "CPU": vm_config["CPU"],
                "RAM": vm_config["RAM"],
                "Disk": vm_config["Disk"],
                "SourceVM": vm_config["SourceVM"],
                "Datastore": vm_config["Datastore"],
                "Network": vm_config["Network"],
                "IP": ip,
                "SubnetMask": vm_config["SubnetMask"],
                "Gateway": vm_config["Gateway"],
                "DNS": vm_config["DNS"]
            }
            new_vm_specs.append(new_vm)

    save_vm_spec_to_db(user_host, new_vm_specs)
    return new_vm_specs

# ------------------- Logs ------------------- #
