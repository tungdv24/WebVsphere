import json
import os

FILES = {
    "1": "deployment_log.json",
    "2": "error.json",
    "3": "system_logs.json"
}

def clear_file(file_path):
    if os.path.exists(file_path):
        with open(file_path, 'w') as f:
            json.dump([], f)
        print(f"{file_path} has been cleared.")
    else:
        print(f"{file_path} does not exist.")

def main():
    print("Select the log file to clear:")
    print("1. deployment_log.json")
    print("2. error.json")
    print("3. system_logs.json")
    choice = input("Enter your choice (1-3): ").strip()

    if choice in FILES:
        clear_file(FILES[choice])
    else:
        print("Invalid choice. Please run the script again and select 1, 2, or 3.")

if __name__ == "__main__":
    main()
