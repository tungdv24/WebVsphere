import requests

ZABBIX_URL = ''
API_TOKEN = ''
TEMPLATES = ["Template OS Linux by Zabbix agent", "Template Module ICMP Ping"]
GROUP_ID = '2'

def zabbix_call_api(method, params, req_id=1):
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "auth": API_TOKEN,
        "id": req_id
    }
    response = requests.post(ZABBIX_URL, json=payload)
    result = response.json()
    if 'error' in result:
        raise Exception(f"Zabbix API error: {result['error']}")
    return result['result']

def get_template_ids(template_names):
    result = zabbix_call_api("template.get", {
        "filter": {"host": template_names}
    })
    return [t['templateid'] for t in result]

def add_host_to_zabbix(hostname, ip):
    template_ids = get_template_ids(TEMPLATES)
    templates = [{"templateid": tid} for tid in template_ids]

    return zabbix_call_api("host.create", {
        "host": hostname,
        "interfaces": [{
            "type": 1,
            "main": 1,
            "useip": 1,
            "ip": ip,
            "dns": "",
            "port": "10050"
        }],
        "groups": [{"groupid": GROUP_ID}],
        "templates": templates,
        "status": 0
    })

def get_zabbix_host_id_by_name(hostname):
    result = zabbix_call_api("host.get", {
        "filter": {"host": [hostname]}
    })
    if result:
        return result[0]['hostid']
    return None

def delete_host_from_zabbix(hostname):
    host_id = get_zabbix_host_id_by_name(hostname)
    if host_id:
        return zabbix_call_api("host.delete", [host_id])
    else:
        raise Exception(f"Zabbix host '{hostname}' not found.")
