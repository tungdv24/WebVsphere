# deploy_v2.py
import ssl
import csv
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import atexit
from time import sleep
from zabbix import add_host_to_zabbix
import json
import os
from datetime import datetime

DEPLOYMENT_LOG_PATH = "deployment_log.json"

def get_obj(content, vimtype, name):
    container = content.viewManager.CreateContainerView(content.rootFolder, vimtype, True)
    for c in container.view:
        if c.name == name:
            return c
    return None

def wait_for_task(task):
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        sleep(2)
    if task.info.state == vim.TaskInfo.State.error:
        raise Exception(task.info.error.msg)

def cidr_to_netmask(cidr):
    cidr = cidr.strip().replace("/", "")
    cidr = int(cidr)
    mask = (0xffffffff >> (32 - cidr)) << (32 - cidr)
    return ".".join([str((mask >> (i * 8)) & 0xff) for i in range(4)[::-1]])

def reconfigure_vm(vm, cpu, ram, disk):
    spec = vim.vm.ConfigSpec()
    spec.numCPUs = int(cpu)
    spec.memoryMB = int(ram) * 1024
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualDisk):
            disk_spec = vim.vm.device.VirtualDeviceSpec()
            disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
            disk_spec.device = dev
            disk_spec.device.capacityInKB = int(disk) * 1024 * 1024
            spec.deviceChange = [disk_spec]
            break
    task = vm.ReconfigVM_Task(spec=spec)
    wait_for_task(task)

def add_disk_to_vm(vm, disk_size_gb, unit_number=None):
    from pyVmomi import vim
    spec = vim.vm.ConfigSpec()
    new_disk_kb = int(disk_size_gb) * 1024 * 1024

    # Find the SCSI controller
    controller = None
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualSCSIController):
            controller = dev
            break

    if not controller:
        raise Exception("No SCSI controller found to attach new disk.")

    # Get next available unit number (skip 7)
    used_units = [dev.unitNumber for dev in vm.config.hardware.device if hasattr(dev, 'unitNumber')]
    next_unit = 0
    while next_unit in used_units or next_unit == 7:
        next_unit += 1

    # Define disk device with no backing config (vSAN will assign default policy)
    disk_spec = vim.vm.device.VirtualDeviceSpec()
    disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    disk_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create

    disk = vim.vm.device.VirtualDisk()
    disk.capacityInKB = new_disk_kb
    disk.unitNumber = unit_number if unit_number is not None else next_unit
    disk.controllerKey = controller.key
    disk.key = -101

    # Empty backing — vSAN handles this based on policy
    disk.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()

    disk_spec.device = disk
    spec.deviceChange = [disk_spec]

    task = vm.ReconfigVM_Task(spec=spec)
    wait_for_task(task)

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
            f.truncate()
            json.dump(logs, f, indent=4)




def deploy_vms_from_csv(vcenter, username, password, csv_file_path):
    context = ssl._create_unverified_context()
    user_identity = f"{username}@{vcenter}"
    si = SmartConnect(host=vcenter, user=username, pwd=password, sslContext=context)
    atexit.register(Disconnect, si)
    content = si.RetrieveContent()

    with open(csv_file_path, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            row = {k: v.strip() if v else '' for k, v in row.items()}
            name = row['name']
            source_vm = get_obj(content, [vim.VirtualMachine], row['source_vm'])
            if not source_vm:
                print(f"Source VM {row['source_vm']} not found.")
                continue

            folder = source_vm.parent
            resource_pool = source_vm.resourcePool
            datastore = get_obj(content, [vim.Datastore], row['datastore'].strip())

            relospec = vim.vm.RelocateSpec()
            relospec.datastore = datastore
            relospec.pool = resource_pool

            clonespec = vim.vm.CloneSpec()
            clonespec.location = relospec
            clonespec.powerOn = False

            # Customization
            adapter1 = vim.vm.customization.AdapterMapping()
            adapter1.adapter = vim.vm.customization.IPSettings()
            adapter1.adapter.ip = vim.vm.customization.FixedIp()
            adapter1.adapter.ip.ipAddress = row['nic1_ip']
            adapter1.adapter.subnetMask = cidr_to_netmask(row['nic1_subnet'])
            adapter1.adapter.gateway = [row['gateway']]

            nic_settings = [adapter1]

            # Only add second NIC if all fields are non-empty
            if row.get('nic2_ip') and row.get('nic2_subnet') and row.get('nic2_network'):
                adapter2 = vim.vm.customization.AdapterMapping()
                adapter2.adapter = vim.vm.customization.IPSettings()
                adapter2.adapter.ip = vim.vm.customization.FixedIp()
                adapter2.adapter.ip.ipAddress = row['nic2_ip']
                adapter2.adapter.subnetMask = cidr_to_netmask(row['nic2_subnet'])
                nic_settings.append(adapter2)

            globalip = vim.vm.customization.GlobalIPSettings()
            globalip.dnsServerList = row['dns'].split(',')

            ident = vim.vm.customization.LinuxPrep()
            ident.hostName = vim.vm.customization.FixedName()
            ident.hostName.name = name
            ident.domain = "local"

            customspec = vim.vm.customization.Specification()
            customspec.nicSettingMap = nic_settings
            customspec.globalIPSettings = globalip
            customspec.identity = ident
            clonespec.customization = customspec

            try:
                yield f"📦 Cloning {name} from {row['source_vm']}..."
                task = source_vm.Clone(folder=folder, name=name, spec=clonespec)
                wait_for_task(task)
                yield f"✅ VM {name} cloned successfully."
                
                new_vm = get_obj(content, [vim.VirtualMachine], name)
                if not new_vm:
                    error_msg = f"❌ Could not find VM {name} after cloning."
                    yield error_msg
                    log_vm_deployment_to_json(user_identity, name, status="❌ Failed", error_message=error_msg)
                    continue

                yield f"🛠️ Reconfiguring {name}..."
                reconfigure_vm(new_vm, row['cpu'], row['ram'], row['disk'])

                yield f"⚡ Powering on {name}..."
                power_task = new_vm.PowerOnVM_Task()
                wait_for_task(power_task)
                yield f"✅ {name} powered on."

                additional_disk = row.get('Additional Disk', '').strip()
                if additional_disk.startswith('+'):
                    try:
                        disk_size = int(additional_disk[1:])
                        yield f"💽 Adding additional disk of {disk_size}GB to {name}..."
                        add_disk_to_vm(new_vm, disk_size)
                        yield f"✅ Additional disk added to {name}."
                    except Exception as e:
                        err = f"❌ Failed to add additional disk to {name}: {e}"
                        yield err

                try:
                    yield f"📡 Registering {name} in Zabbix..."
                    zabbix_ip = row.get('zabbix_ip', row['nic1_ip'])
                    add_host_to_zabbix(name, zabbix_ip)
                    yield f"✅ {name} registered in Zabbix."
                except Exception as e:
                    err = f"❌ Failed to register {name} in Zabbix: {e}"
                    yield err  # Log but do not fail the whole deployment

                log_vm_deployment_to_json(user_identity, name, status="✅ Success")

            except Exception as e:
                error_msg = f"❌ Deployment failed: {str(e)}"
                yield error_msg
                log_vm_deployment_to_json(user_identity, name, status="❌ Failed", error_message=str(e))

        
        yield "✅ All deployments completed successfully."