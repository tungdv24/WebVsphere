# deploy.py
import json
import sqlite3
from pyVmomi import vim
from pyVim.connect import Disconnect
from vsphere_session import get_session
import time
from flask import session
from log import log_system_action, log_error
from vsphere_session import connect_to_vcenter, save_session, login_required
from datetime import datetime
import os
from zabbix import add_host_to_zabbix

DEPLOYMENT_LOG_PATH = "deployment_log.json"

def log_vm_deployment_to_json(user, vm_name, status="✅ Success", error_message=None):
    log_entry = {
        "user": user,
        "vm_name": vm_name,
        "status": status,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    if status != "✅ Success" and error_message:
        log_entry["error"] = error_message

    if not os.path.exists(DEPLOYMENT_LOG_PATH):
        with open(DEPLOYMENT_LOG_PATH, 'w') as f:
            json.dump([log_entry], f, indent=4)
    else:
        with open(DEPLOYMENT_LOG_PATH, 'r+') as f:
            try:
                logs = json.load(f)
            except json.JSONDecodeError:
                logs = []

            logs.append(log_entry)
            f.seek(0)
            json.dump(logs, f, indent=4)

# -------- DB Helper --------
def get_user_host():
    user = session.get('username', 'default_user')
    host = session.get('vcenter_host', 'default_host')
    return f"{user}_{host}"

def load_vm_specs_from_db():
    user_host = get_user_host()
    conn = sqlite3.connect("vm_data.db")
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT spec_json FROM vm_data WHERE user_host = ?", (user_host,))
        result = cursor.fetchone()
        if result:
            return json.loads(result[0])
        else:
            return []
    finally:
        conn.close()

# -------- Clone Functions --------
def get_vm_by_name(content, vm_name):
    obj_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        for vm in obj_view.view:
            if vm.name == vm_name:
                return vm
        return None
    finally:
        obj_view.Destroy()

def get_datastore(content, datastore_name):
    obj_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datastore], True)
    try:
        for datastore in obj_view.view:
            if datastore.name == datastore_name:
                return datastore
        raise ValueError(f"Datastore '{datastore_name}' not found!")
    finally:
        obj_view.Destroy()

def get_network(content, network_name):
    obj_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Network], True)
    try:
        for network in obj_view.view:
            if network.name == network_name:
                return network
        raise ValueError(f"Network '{network_name}' not found!")
    finally:
        obj_view.Destroy()

def create_disk_and_network_changes(source_vm, content, vm_config):
    changes = []

    for device in source_vm.config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualDisk):
            disk_spec = vim.vm.device.VirtualDeviceSpec()
            disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
            disk_spec.device = device
            disk_spec.device.capacityInKB = vm_config['Disk'] * 1024 * 1024
            changes.append(disk_spec)

    network = get_network(content, vm_config['Network'])
    nics = [dev for dev in source_vm.config.hardware.device if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
    if not nics:
        raise ValueError("No network interface card found on source VM.")

    nic_spec = vim.vm.device.VirtualDeviceSpec()
    nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
    nic_spec.device = nics[0]
    nic_spec.device.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
        network=network, deviceName=vm_config['Network']
    )
    nic_spec.device.addressType = "generated"
    changes.append(nic_spec)

    return changes

def create_customization_spec(content, vm_config):
    global_ip = vim.vm.customization.GlobalIPSettings()
    global_ip.dnsServerList = vm_config['DNS']

    adapter_mapping = vim.vm.customization.AdapterMapping()
    adapter_mapping.adapter = vim.vm.customization.IPSettings(
        ip=vim.vm.customization.FixedIp(ipAddress=vm_config['IP']),
        subnetMask=vm_config['SubnetMask'],
        gateway=vm_config['Gateway']
    )

    ident = vim.vm.customization.LinuxPrep(
        hostName=vim.vm.customization.FixedName(name=vm_config['NameVM']),
        domain="local"
    )

    custom_spec = vim.vm.customization.Specification()
    custom_spec.nicSettingMap = [adapter_mapping]
    custom_spec.globalIPSettings = global_ip
    custom_spec.identity = ident

    return custom_spec

def clone_vm_stream(content, source_vm_name, vm_config):
    try:
        source_vm = get_vm_by_name(content, source_vm_name)
        if not source_vm:
            yield f"data: ❌ Source VM '{source_vm_name}' not found\n\n"
            return

        dest_folder = source_vm.parent
        resource_pool = source_vm.resourcePool

        relocate_spec = vim.vm.RelocateSpec()
        relocate_spec.datastore = get_datastore(content, vm_config['Datastore'])
        relocate_spec.pool = resource_pool

        config_spec = vim.vm.ConfigSpec()
        config_spec.numCPUs = vm_config['CPU']
        config_spec.memoryMB = vm_config['RAM'] * 1024
        config_spec.deviceChange = create_disk_and_network_changes(source_vm, content, vm_config)

        clone_spec = vim.vm.CloneSpec()
        clone_spec.location = relocate_spec
        clone_spec.powerOn = True
        clone_spec.config = config_spec
        clone_spec.customization = create_customization_spec(content, vm_config)

        task = source_vm.Clone(name=vm_config['NameVM'], folder=dest_folder, spec=clone_spec)

        # Inline wait for task logic (replaces wait_for_task_stream)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            yield f"data: ⏳ Cloning task status: {task.info.state}\n\n"

        if task.info.state == vim.TaskInfo.State.success:
            yield f"data: ✅ VM '{vm_config['NameVM']}' cloned successfully!\n\n"
        else:
            error_msg = task.info.error.localizedMessage if task.info.error else "Unknown error"
            yield f"data: ❌ Clone failed for '{vm_config['NameVM']}': {error_msg}\n\n"

    except Exception as e:
        yield f"data: ❌ Error cloning '{vm_config.get('NameVM', 'Unknown')}': {str(e)}\n\n"



# -------- Logging Function --------
def log_successful_vm_creation(username, vcenter_host, vm_name):
    conn = sqlite3.connect("vm_data.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO vm_logs (username, vcenter_host, vm_name, created_at)
        VALUES (?, ?, ?, datetime('now'))
    """, (username, vcenter_host, vm_name))
    conn.commit()
    conn.close()

# -------- Main Function --------
def deploy_vms_from_config_stream(session_id):
    yield "event: status\ndata: 🚀 Starting VM deployment...\n\n"

    # Attempt to reuse existing vCenter session or reconnect
    si = get_session(session_id)
    if not si:
        try:
            vcenter_host = session.get('vcenter_host')
            username = session.get('username')
            password = session.get('password')

            if not all([vcenter_host, username, password]):
                raise Exception("Missing vSphere credentials in session.")

            si = connect_to_vcenter(vcenter_host, username, password)
            save_session(session_id, si)
            yield "event: status\ndata: 🔄 Reconnected to vSphere successfully.\n\n"
        except Exception as e:
            yield f"event: error\ndata: ❌ Reconnection failed: {str(e)}\n\n"
            return

    try:
        content = si.RetrieveContent()
        vm_configs = load_vm_specs_from_db()

        if not vm_configs:
            yield "event: error\ndata: ⚠️ No VM specs found in database.\n\n"
            return

        username = session.get('username', 'default_user')
        vcenter_host = session.get('vcenter_host', 'default_host')
        user_identity = f"{username}@{vcenter_host}"

        for i, vm_config in enumerate(vm_configs, 1):
            name = vm_config.get('NameVM', 'Unknown')
            yield f"event: status\ndata: 📦 Deploying VM {i}/{len(vm_configs)}: {name}\n\n"

            try:
                yield f"event: status\ndata: 🔍 Locating source VM '{vm_config['SourceVM']}'...\n\n"
                if not get_vm_by_name(content, vm_config['SourceVM']):
                    raise Exception(f"Source VM '{vm_config['SourceVM']}' not found.")
                yield "event: status\ndata: ✅ Source VM found\n\n"

                yield f"event: status\ndata: 📦 Verifying datastore '{vm_config['Datastore']}'...\n\n"
                get_datastore(content, vm_config['Datastore'])
                yield "event: status\ndata: ✅ Datastore verified\n\n"

                yield f"event: status\ndata: 🌐 Verifying network '{vm_config['Network']}'...\n\n"
                get_network(content, vm_config['Network'])
                yield "event: status\ndata: ✅ Network verified\n\n"

                yield "event: status\ndata: 🛠️ Cloning in progress...\n\n"

                # Streaming VM cloning process and checking for success
                success = False
                for msg in clone_vm_stream(content, vm_config['SourceVM'], vm_config):
                    yield msg
                    if f"✅ VM '{vm_config['NameVM']}' cloned successfully!" in msg:
                        success = True

                if success:
                    log_successful_vm_creation(username, vcenter_host, vm_config['NameVM'])
                    log_vm_deployment_to_json(user_identity, vm_config['NameVM'], status="✅ Success")
                    yield f"event: status\ndata: ✅ VM '{vm_config['NameVM']}' deployed successfully!\n\n"
                    try:
                        add_host_to_zabbix(vm_config['NameVM'], vm_config['IP'])
                        yield f"event: status\ndata: 🧩 Zabbix host added: {vm_config['NameVM']} ({vm_config['IP']})\n\n"
                    except Exception as ze:
                        log_error(str(ze), context="Zabbix Integration")
                        yield f"event: error\ndata: ⚠️ Failed to add Zabbix host for {vm_config['NameVM']}: {str(ze)}\n\n"

                else:
                    log_vm_deployment_to_json(user_identity, vm_config['NameVM'], status="❌ Failed", error_message="Unknown error during cloning")
                    yield f"event: error\ndata: ❌ Deployment failed or unclear for '{vm_config['NameVM']}'\n\n"

            except Exception as e:
                log_error(str(e), context=f"VM: {name}")
                log_vm_deployment_to_json(user_identity, name, status="❌ Failed", error_message=str(e))
                yield f"event: error\ndata: ❌ Failed to deploy '{name}': {str(e)}\n\n"

        yield "event: done\ndata: ✅ All VMs processed.\n\n"

    except Exception as e:
        log_error(str(e), context="deploy_vms_from_config_stream")
        yield f"event: error\ndata: ❌ Unexpected error: {str(e)}\n\n"





