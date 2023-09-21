import aiosqlite


class DataBase:
    async def connect_db(self):
        self.conn = await aiosqlite.connect("note.db")
        return self.conn

    async def close_db(self):
        await self.conn.commit()
        await self.conn.close()

    async def create_db(self):
        db = await self.connect_db()
        await db.execute("CREATE TABLE IF NOT EXISTS notes (ID INTEGER PRIMARY KEY,note_name TEXT,note_desc TEXT)")
        await self.close_db()


database = DataBase()


async def add_note(NoteName, NoteDesc):
    db = await database.connect_db()
    await db.execute("INSERT INTO notes (note_name,note_desc) VALUES (?,?)", (NoteName, NoteDesc,))
    await database.close_db()


async def list_note():
    db = await database.connect_db()
    cur = await db.execute("SELECT ID, note_name, note_desc FROM notes")
    cur = await cur.fetchall()
    await database.close_db()
    return cur


async def get_note_by_id(ids):
    db = await database.connect_db()
    cur = await db.execute("SELECT note_name, note_desc FROM notes WHERE ID = ?", (ids,))
    cur = await cur.fetchone()
    await database.close_db()
    print(cur)
    return cur


async def delete_note(ids):
    db = await database.connect_db()
    await db.execute("DELETE FROM notes WHERE ID = ?", (ids,))
    await database.close_db()


async def find_note_by_text(text):
    db = await database.connect_db()
    results = await db.execute("SELECT ID FROM notes WHERE note_desc LIKE ?", ('%' + text + '%',))
    results = await results.fetchall()
    if results:
        return results
    else:
        return False


async def delete_all():
    db = await database.connect_db()
    await db.execute("DELETE FROM notes")
    await database.close_db()
