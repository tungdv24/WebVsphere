import sqlite3

def init_db(db_name='vm_data.db'):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vm_data (
            user_host TEXT PRIMARY KEY,
            config_json TEXT,
            spec_json TEXT
        )
    ''')

    conn.commit()
    conn.close()
    print(f"Database '{db_name}' initialized with table 'vm_data'.")

if __name__ == "__main__":
    init_db()
