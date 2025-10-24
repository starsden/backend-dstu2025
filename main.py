from fastapi import FastAPI
from pydantic import BaseModel
from uuid import uuid4
import asyncio
import time
import httpx
import socket
import dns.resolver

app = FastAPI(title="Aeza Host Checker")

tasks = {}
results = {}

class CheckRequest(BaseModel):
    target: str
    type: str
    port: int | None = None
    record_type: str | None = None

class CheckResult(BaseModel):
    id: str
    status: str
    code: int | None = None
    response_time: float | None = None
    data: dict | None = None
    error: str | None = None

queue = asyncio.Queue()
@app.post("/api/checks")
async def checkalka(req: CheckRequest):
    task_id = str(uuid4())
    task = req.model_dump()
    task["id"] = task_id
    tasks[task_id] = task
    await queue.put(task)
    return {"id": task_id, "status": "queued"}


@app.get("/api/checks/{task_id}")
async def getcheck(task_id: str):
    if task_id not in tasks:
        return {"error": "Task not found"}
    if task_id not in results:
        return {"id": task_id, "status": "pending"}
    return results[task_id]


@app.on_event("startup")
async def worker():
    async def taski():
        while True:
            task = await queue.get()
            start = time.time()

            try:
                # HTTP
                if task["type"] == "http":
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        r = await client.get(task["target"], timeout=5)
                        results[task["id"]] = {
                            "id": task["id"],
                            "status": "ok" if r.status_code < 400 else "fail",
                            "code": r.status_code,
                            "response_time": time.time() - start,
                            "data": {
                                "headers": dict(r.headers),
                                "url": str(r.url)
                            },
                            "error": None
                        }

                # PING
                elif task["type"] == "ping":
                    host = task["target"]
                    proc = await asyncio.create_subprocess_shell(
                        f"ping -c 1 {host}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await proc.communicate()
                    success = proc.returncode == 0
                    response_time = None
                    if success and b"time=" in stdout:
                        line = stdout.decode().split("time=")[1]
                        response_time = float(line.split(" ")[0])
                    results[task["id"]] = {
                        "id": task["id"],
                        "status": "ok" if success else "fail",
                        "code": 0,
                        "response_time": response_time,
                        "data": {"output": stdout.decode()},
                        "error": None if success else stderr.decode()
                    }

                # TCP
                elif task["type"] == "tcp":
                    host, port = task["target"], task.get("port", 80)
                    start = time.time()
                    try:
                        reader, writer = await asyncio.open_connection(host, port)
                        writer.close()
                        await writer.wait_closed()
                        success = True
                    except Exception as e:
                        success = False
                        raise e
                    results[task["id"]] = {
                        "id": task["id"],
                        "status": "ok" if success else "fail",
                        "response_time": time.time() - start,
                        "data": {"host": host, "port": port},
                        "error": None if success else "Connection failed"
                    }

                # TRACEROUTE
                elif task["type"] == "traceroute":
                    host = task["target"]
                    proc = await asyncio.create_subprocess_shell(
                        f"traceroute -m 15 {host}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await proc.communicate()
                    success = proc.returncode == 0
                    results[task["id"]] = {
                        "id": task["id"],
                        "status": "ok" if success else "fail",
                        "response_time": time.time() - start,
                        "data": {"trace": stdout.decode().splitlines()},
                        "error": None if success else stderr.decode()
                    }

                # DNS
                elif task["type"] == "dns":
                    host = task["target"]
                    record_type = task.get("record_type", "A").upper()
                    resolver = dns.resolver.Resolver()
                    answer = resolver.resolve(host, record_type)
                    data = [rdata.to_text() for rdata in answer]
                    results[task["id"]] = {
                        "id": task["id"],
                        "status": "ok",
                        "response_time": time.time() - start,
                        "data": {"records": data, "type": record_type},
                        "error": None
                    }

            except Exception as e:
                results[task["id"]] = {
                    "id": task["id"],
                    "status": "error",
                    "code": None,
                    "response_time": None,
                    "data": None,
                    "error": str(e)
                }

            finally:
                queue.task_done()

    asyncio.create_task(taski())
