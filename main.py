from fastapi import FastAPI, Depends
from pydantic import BaseModel
from uuid import uuid4
import asyncio, time, httpx, dns.resolver, json
import redis.asyncio as redis
import secrets

from sqlalchemy import insert
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import Base, engine, get_db
from models import Task, Result, Agents

app = FastAPI(title="dns check")

redis_client = redis.Redis(host='localhost', port=6379, db=0, encoding="utf-8", decode_responses=True)

async def checkalka_redisa():
    try:
        await redis_client.ping()
        print("✅ Connected to Redis successfully")
    except Exception as e:
        raise RuntimeError(f"Redis connection failed: {e}")

class CheckRequest(BaseModel):
    target: str
    type: str
    port: int | None = None
    record_type: str | None = None

class AgentRegisterRequest(BaseModel):
    name: str
    desc: str
    email: str

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await checkalka_redisa()
    for i in range(5):
        asyncio.create_task(worker(i))

@app.post("/api/checks")
async def checkkk(req: CheckRequest, db: AsyncSession = Depends(get_db)):
    task_id = str(uuid4())

    if req.type == "full":
        group_id = task_id
        checks = [
            {"type": "ping"},
            {"type": "http"},
            {"type": "tcp"},
            {"type": "traceroute"},
            {"type": "dns"}
        ]
        for ch in checks:
            sub_id = str(uuid4())
            new_task = Task(id=sub_id, target=req.target, type=ch["type"],
                            port=req.port, record_type=req.record_type)
            db.add(new_task)
            await db.commit()

            task_data = {
                "id": sub_id,
                "group_id": group_id,
                "target": req.target,
                "type": ch["type"],
                "port": req.port,
                "record_type": req.record_type
            }
            await redis_client.lpush("task_queue", json.dumps(task_data))
        return {"id": group_id, "status": "queued", "parts": len(checks)}

    new_task = Task(id=task_id, target=req.target, type=req.type,
                    port=req.port, record_type=req.record_type)
    db.add(new_task)
    await db.commit()

    task = req.model_dump() | {"id": task_id}
    await redis_client.lpush("task_queue", json.dumps(task))
    return {"id": task_id, "status": "queued"}

@app.get("/api/checks/{task_id}")
async def get_check(task_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Result).where(Result.id == task_id))
    rec = result.scalar()
    if rec:
        return {
            "id": rec.id,
            "status": rec.status,
            "code": rec.code,
            "response_time": rec.response_time,
            "data": rec.data,
            "error": rec.error
        }

    results = await db.execute(select(Result).where(Result.data['group_id'].as_string() == task_id))
    res_list = results.scalars().all()
    if res_list:
        return {
            "id": task_id,
            "status": "completed" if all(r.status != "pending" for r in res_list) else "pending",
            "results": [
                {
                    "type": r.data.get("type"),
                    "status": r.status,
                    "code": r.code,
                    "response_time": r.response_time,
                    "data": r.data,
                    "error": r.error
                }
                for r in res_list
            ]
        }

    return {"id": task_id, "status": "pending"}
async def worker(worker_id: int):
    async with AsyncSession(engine) as db:
        while True:
            task_json = await redis_client.brpop("task_queue", timeout=5)
            if not task_json:
                continue

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
                        err = None
                    except Exception as e:
                        ok = False
                        err = str(e)
                    db.add(Result(id=task["id"], status="ok" if ok else "fail",
                                  response_time=time.time()-start,
                                  data={"host": host, "port": port},
                                  error=err))
                    await db.commit()

                elif task["type"] == "traceroute":
                    proc = await asyncio.create_subprocess_shell(
                        f"traceroute -m 10 -w 2 {task['target']}",
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


@app.post("/api/agents/register")
async def register_agent(req: AgentRegisterRequest, db: AsyncSession = Depends(get_db)):
    agent_id = str(uuid4())
    api_key = secrets.token_hex(16)
    status = "Active"

    new_agent = Agents(
        id=agent_id,
        status=status,
        name=req.name,
        desc=req.desc,
        email=req.email,
        api=api_key
    )

    db.add(new_agent)
    await db.commit()

    return {
        "id": agent_id,
        "status": status,
        "api_key": api_key,
        "name": req.name,
        "desc": req.desc,
        "email": req.email
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
