import sqlite3
import json

def view_data(db_name='vm_data.db'):
    conn = sqlite3.connect(db_name)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM vm_data')
    rows = cursor.fetchall()

    if not rows:
        print("No data found.")
        return

    for row in rows:
        print("=" * 50)
        print(f"User@Host: {row['user_host']}")
        print("Config JSON:")
        print(json.dumps(json.loads(row['config_json']), indent=4))
        print("Spec JSON:")
        print(json.dumps(json.loads(row['spec_json']), indent=4))
        print("=" * 50 + "\n")

    conn.close()

if __name__ == "__main__":
    view_data()
