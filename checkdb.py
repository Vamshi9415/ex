import sqlite3

c = sqlite3.connect("data/valura_sessions.db")
print(c.execute("select name from sqlite_master where type='table'").fetchall())