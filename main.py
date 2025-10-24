from fastapi import FastAPI, Depends
from pydantic import BaseModel
from uuid import uuid4
import asyncio, time, httpx, dns.resolver, json
import redis.asyncio as redis

from sqlalchemy import Column, String, Float, JSON, Text
from sqlalchemy.future import select
from sqlalchemy.dialects.sqlite import BLOB
from sqlalchemy.ext.asyncio import AsyncSession
from database import Base, engine, get_db
from models import Task, Result

app = FastAPI(title="dns check")

redis_client = redis.Redis(host='localhost', port=6379, db=0, encoding="utf-8", decode_responses=True)

async def check_redis_connection():
    try:
        await redis_client.ping()
        print("Connected to Redis successfully")
    except Exception as e:
        raise

class CheckRequest(BaseModel):
    target: str
    type: str
    port: int | None = None
    record_type: str | None = None

@app.on_event("startup")
async def startup_event():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await check_redis_connection()
    asyncio.create_task(worker())

@app.post("/api/checks")
async def create_check(req: CheckRequest, db: AsyncSession = Depends(get_db)):
    task_id = str(uuid4())
    new_task = Task(id=task_id, target=req.target, type=req.type,
                    port=req.port, record_type=req.record_type)
    db.add(new_task)
    await db.commit()
    task = req.model_dump() | {"id": task_id}
    await redis_client.lpush("task_queue", json.dumps(task))
    print(f"Task {task_id} added to task_queue")
    return {"id": task_id, "status": "queued"}

@app.get("/api/checks/{task_id}")
async def get_check(task_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Result).where(Result.id == task_id))
    rec = result.scalar()
    if not rec:
        return {"id": task_id, "status": "pending"}
    return {
        "id": rec.id,
        "status": rec.status,
        "code": rec.code,
        "response_time": rec.response_time,
        "data": rec.data,
        "error": rec.error
    }

async def worker():
    async def process():
        async with AsyncSession(engine) as db:
            while True:
                task_json = await redis_client.brpop("task_queue", timeout=5)
                if task_json:
                    task = json.loads(task_json[1])
                    start = time.time()
                    try:
                        if task["type"] == "http":
                            async with httpx.AsyncClient(follow_redirects=True) as client:
                                r = await client.get(task["target"], timeout=5)
                                data = {"headers": dict(r.headers), "url": str(r.url)}
                                status = "ok" if r.status_code < 400 else "fail"
                                db.add(Result(id=task["id"], status=status,
                                              code=r.status_code,
                                              response_time=time.time()-start,
                                              data=data))
                                await db.commit()

                        elif task["type"] == "ping":
                            proc = await asyncio.create_subprocess_shell(
                                f"ping -c 1 {task['target']}",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE)
                            out, err = await proc.communicate()
                            ok = proc.returncode == 0
                            resp_time = None
                            if ok and b"time=" in out:
                                line = out.decode().split("time=")[1]
                                resp_time = float(line.split(" ")[0])
                            db.add(Result(id=task["id"], status="ok" if ok else "fail",
                                          response_time=resp_time,
                                          data={"output": out.decode()},
                                          error=None if ok else err.decode()))
                            await db.commit()

                        elif task["type"] == "tcp":
                            host, port = task["target"], task.get("port", 80)
                            try:
                                reader, writer = await asyncio.open_connection(host, port)
                                writer.close()
                                await writer.wait_closed()
                                ok = True
                            except Exception as e:
                                ok = False
                                err = str(e)
                            db.add(Result(id=task["id"], status="ok" if ok else "fail",
                                          response_time=time.time()-start,
                                          data={"host": host, "port": port},
                                          error=None if ok else err))
                            await db.commit()

                        elif task["type"] == "traceroute":
                            proc = await asyncio.create_subprocess_shell(
                                f"traceroute -m 15 {task['target']}",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE)
                            out, err = await proc.communicate()
                            ok = proc.returncode == 0
                            db.add(Result(id=task["id"], status="ok" if ok else "fail",
                                          response_time=time.time()-start,
                                          data={"trace": out.decode().splitlines()},
                                          error=None if ok else err.decode()))
                            await db.commit()

                        elif task["type"] == "dns":
                            resolver = dns.resolver.Resolver()
                            rt = task.get("record_type", "A").upper()
                            answer = resolver.resolve(task["target"], rt)
                            data = [rdata.to_text() for rdata in answer]
                            db.add(Result(id=task["id"], status="ok",
                                          response_time=time.time()-start,
                                          data={"records": data, "type": rt}))
                            await db.commit()

                    except Exception as e:
                        db.add(Result(id=task["id"], status="error", error=str(e)))
                        await db.commit()
                    await asyncio.sleep(5)

    asyncio.create_task(process())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)