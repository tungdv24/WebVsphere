# vsphere_session.py
import ssl
from pyVim import connect
from pyVmomi import vim
from threading import Lock
import concurrent.futures
from functools import wraps
from flask import session, redirect, url_for, jsonify


_sessions = {}
_lock = Lock()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def connect_to_vcenter(host, user, password, timeout=7):
    def _connect():
        return connect.SmartConnect(
            host=host,
            user=user,
            pwd=password,
            sslContext=ssl._create_unverified_context()
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_connect)
        try:
            si = future.result(timeout=timeout)
            return si
        except concurrent.futures.TimeoutError:
            print("❌ Failed to connect to vCenter: connection timed out.")
            raise Exception("Connection to vCenter timed out after 7 seconds.")
        except Exception as e:
            print(f"❌ Failed to connect to vCenter: {e}")
            return None

def connect_vsphere_from_session(session):
    try:
        host = session.get('vcenter_host')
        user = session.get('username')
        password = session.get('password')
        if not host or not user or not password:
            raise ValueError("Missing vSphere credentials in session")

        return connect_to_vcenter(host, user, password)
    except Exception as e:
        print(f"[vSphere] Session connection failed: {e}")
        return None
    
def save_session(session_id, si):
    with _lock:
        _sessions[session_id] = si

def get_session(session_id):
    with _lock:
        return _sessions.get(session_id, None)

def disconnect_session(session_id):
    with _lock:
        si = _sessions.pop(session_id, None)
        if si:
            connect.Disconnect(si)
            print(f"[vSphere] Disconnected session: {session_id}")
