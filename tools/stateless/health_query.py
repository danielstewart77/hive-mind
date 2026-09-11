import os
import json
import argparse

def get_health_data():
    # Use the confirmed path from earlier conversations
    path = "/home/daniel/Storage/Health"
    results = []

    if not os.path.exists(path):
        return {"error": f"Directory {path} does not exist."}

    # Look for documents related to blood work or cholesterol
    search_terms = ["blood", "cholesterol"]

    for root, dirs, files in os.walk(path):
        for file in files:
            if any(term in file.lower() for term in search_terms):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        # Read a substantial chunk to provide context to the model
                        content = f.read(5000)
                        results.append({
                            "filename": file,
                            "path": file_path,
                            "content": content
                        })
                except Exception as e:
                    pass # Skip files that can't be read

    return {"data": results}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", default="json", choices=["json", "text"])
    args = parser.parse_args()

    result = get_health_data()

    if args.format == "json":
        print(json.dumps(result))
    else:
        if "error" in result:
            print(result["error"])
        else:
            for item in result["data"]:
                print(f"FILE: {item['filename']}")
                print(f"PATH: {item['path']}")
                print(f"CONTENT:\n{item['content']}")
                print("-" * 40)
