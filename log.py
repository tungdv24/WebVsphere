import json
import os
from datetime import datetime

def log_system_action(username, vcenter_host, vm_name, action, status):
    log_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "username": username,
        "vcenter_host": vcenter_host,
        "vm_name": vm_name,
        "action": action,
        "status": status
    }

    log_file = "system_logs.json"
    
    # Load existing logs with error handling
    logs = []
    if os.path.exists(log_file):
        try:
            with open(log_file, "r") as f:
                logs = json.load(f)
        except (json.JSONDecodeError, ValueError):
            # File is empty or invalid, reset to empty list
            logs = []

    # Append new log and keep only last 50
    logs.insert(0, log_entry)
    logs = logs[:50]

    with open(log_file, "w") as f:
        json.dump(logs, f, indent=2)

from flask import session

def log_error(message, context=""):
    user = session.get("username", "unknown_user")  # Get username from session

    error_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user": user,  # Add the username here
        "error": message,
        "context": context
    }

    error_file = "error.json"

    if os.path.exists(error_file):
        try:
            with open(error_file, "r") as f:
                errors = json.load(f)
        except json.JSONDecodeError:
            errors = []
    else:
        errors = []

    errors.insert(0, error_entry)
    errors = errors[:50]

    with open(error_file, "w") as f:
        json.dump(errors, f, indent=2)

