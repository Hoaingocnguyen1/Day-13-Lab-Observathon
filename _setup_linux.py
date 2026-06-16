import zipfile, os, stat

dest = 'bin/practice'
os.makedirs(dest, exist_ok=True)
z = zipfile.ZipFile('observathon-practice-linux-x64.zip')
z.extract('observathon-sim', dest)
p = os.path.join(dest, 'observathon-sim')
os.chmod(p, 0o755)
print("extracted ->", p, os.path.getsize(p), "bytes, mode", oct(os.stat(p).st_mode & 0o777))
