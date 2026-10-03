import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT COALESCE(MAX(element_idx),-1) FROM ia_plan").fetchone()[0])
