import os
import csv
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, send_from_directory
from werkzeug.utils import secure_filename
from deploy_v2 import deploy_vms_from_csv
from vsphere_session import login_required
from flask import Response, stream_with_context


clone_v2_bp = Blueprint('clone_v2', __name__, template_folder='templates')

BASE_UPLOAD_FOLDER = 'uploads'

def get_user_folder():
    user = session.get('username')
    host = session.get('vcenter_host')
    if not user or not host:
        return None
    folder_name = f"{user}@{host}".replace(" ", "_")
    return os.path.join(BASE_UPLOAD_FOLDER, folder_name)

def list_user_csv_files():
    folder = get_user_folder()
    if not folder or not os.path.exists(folder):
        return []
    return [f for f in os.listdir(folder) if f.endswith('.csv')]

def parse_csv(filepath):
    with open(filepath, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        return list(reader)

@clone_v2_bp.route('/', methods=['GET', 'POST'])
def clone_v2():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    user_dir = get_user_folder()
    os.makedirs(user_dir, exist_ok=True)

    csv_files = os.listdir(user_dir)
    selected_data = None

    if request.method == 'POST':
        if 'csvfile' in request.files:
            file = request.files['csvfile']
            if file.filename.endswith('.csv'):
                filename = secure_filename(file.filename)
                path = os.path.join(user_dir, filename)
                file.save(path)
                flash(f"Uploaded {filename}")
                return redirect(url_for('clone_v2.clone_v2'))

        elif 'run_file' in request.form:
            filename = request.form['run_file']
            filepath = os.path.join(user_dir, filename)
            try:
                result = deploy_vms_from_csv(
                    vcenter=session['vcenter_host'],
                    username=session['username'],
                    password=session['password'],
                    csv_file_path=filepath
                )
                flash(f"✅ {result}")
            except Exception as e:
                flash(f"❌ Error: {e}")
            return redirect(url_for('clone_v2.clone_v2'))

        elif 'delete_file' in request.form:
            filename = request.form['delete_file']
            os.remove(os.path.join(user_dir, filename))
            flash(f"Deleted {filename}")
            return redirect(url_for('clone_v2.clone_v2'))

        elif 'select_file' in request.form:
            filename = request.form['select_file']
            filepath = os.path.join(user_dir, filename)
            selected_data = parse_csv(filepath)

    return render_template('clone_v2.html', csv_files=csv_files, vm_data=selected_data)


@clone_v2_bp.route('/stream_deploy/<filename>')
@login_required
def stream_deploy(filename):
    user_dir = get_user_folder()
    filepath = os.path.join(user_dir, filename)

    def generate():
        for line in deploy_vms_from_csv(
            vcenter=session['vcenter_host'],
            username=session['username'],
            password=session['password'],
            csv_file_path=filepath
        ):
            yield f"data: {line}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@clone_v2_bp.route('/download_csv/<filename>')
@login_required
def download_csv(filename):
    user_folder = get_user_folder()
    return send_from_directory(user_folder, filename, as_attachment=True)
