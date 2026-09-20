import os, sqlite3
print("listdir", os.listdir("/data/s3-live-monitor"))
for p in ("/data/s3-live-monitor/manifest.db", "/app/data/manifest.db"):
    print("exists", p, os.path.exists(p), os.path.getsize(p) if os.path.exists(p) else None)
c = sqlite3.connect("/data/s3-live-monitor/manifest.db")
print("tables", c.execute("select name from sqlite_master where type='table'").fetchall())
