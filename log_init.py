import sqlite3

def create_logs_table():
    conn = sqlite3.connect("vm_data.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vm_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            vcenter_host TEXT NOT NULL,
            vm_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

if __name__ == "__main__":
    create_logs_table()
    print("✅ vm_logs table created.")
