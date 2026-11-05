#!/usr/bin/env python3
"""state.db (SQLite) <-> state.json.

  python3 scripts/state_json.py dump state.db state.json   — выгрузить всё в читаемый JSON
  python3 scripts/state_json.py load state.json state.db   — собрать state.db обратно из JSON

В JSON лежат схема таблиц (CREATE ...) и все строки, поэтому восстановление точное.
"""
import json
import sqlite3
import sys
import time


def dump(db_path, json_path):
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("BEGIN")  # одна согласованная «фотография» базы
    objs = con.execute(
        "select type, name, tbl_name, sql from sqlite_master "
        "where sql is not null and name not like 'sqlite_%' order by type desc, name"
    ).fetchall()
    tables = [o for o in objs if o[0] == "table"]
    extras = [o[3] for o in objs if o[0] != "table"]  # индексы и т.п.
    with open(json_path, "w", encoding="utf-8") as f:
        f.write('{\n  "version": 1,\n  "saved_at": %s,\n  "tables": {\n' % json.dumps(
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        for i, (_, name, _, sql) in enumerate(tables):
            cur = con.execute(f'select * from "{name}"')
            cols = [c[0] for c in cur.description]
            f.write(f'    {json.dumps(name)}: {{\n      "sql": {json.dumps(sql, ensure_ascii=False)},\n')
            f.write(f'      "columns": {json.dumps(cols)},\n      "rows": [')
            first = True
            for row in cur:
                f.write(("\n        " if first else ",\n        ") + json.dumps(list(row), ensure_ascii=False))
                first = False
            f.write("\n      ]\n    }" + ("," if i < len(tables) - 1 else "") + "\n")
        f.write('  },\n  "extra_sql": %s\n}\n' % json.dumps(extras, ensure_ascii=False))
    con.rollback()
    con.close()


def load(json_path, db_path):
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    con = sqlite3.connect(db_path)
    for name, t in data["tables"].items():
        con.execute(t["sql"])
        if t["rows"]:
            q = ",".join("?" * len(t["columns"]))
            cols = ",".join(f'"{c}"' for c in t["columns"])
            con.executemany(f'insert into "{name}"({cols}) values({q})', t["rows"])
    for sql in data.get("extra_sql", []):
        con.execute(sql)
    con.commit()
    con.close()


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("dump", "load"):
        sys.exit(__doc__)
    (dump if sys.argv[1] == "dump" else load)(sys.argv[2], sys.argv[3])
