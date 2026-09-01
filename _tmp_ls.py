import os
root = r"D:\Dat\POC\audio\src"
for base, dirs, files in os.walk(root):
    for f in sorted(files):
        if f.endswith(".py") and "cache" not in base:
            print(os.path.relpath(os.path.join(base, f), root))