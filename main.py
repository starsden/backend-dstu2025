from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from uuid import uuid4
import asyncio, time, httpx, dns.resolver, json
import redis.asyncio as redis
import secrets
import os
import locale


from passlib.context import CryptContext
from datetime import datetime, timedelta
import jwt
from fastapi.security import OAuth2PasswordBearer
from keys import secretik
from sqlalchemy import insert
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import Base, engine, get_db
from models import Task, Result, Agents, ActiveAgents, Admin
from smtp import send_api
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Column, String
from fastapi.responses import JSONResponse


pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

SECRET_KEY = secretik
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")
app = FastAPI(title="Aeza x Culture Union", description="API для проверки DNS записей и не только", version="1.0.0", docs_url="/papers")

active_agents: dict[str, WebSocket] = {}
redis_client = redis.Redis(host='localhost', port=6379, db=0, encoding="utf-8", decode_responses=True)
# redis_client = redis.Redis(host=os.getenv('REDIS_HOST', 'localhost'), port=6379, db=0, encoding="utf-8", decode_responses=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8501",
        "https://checkpulse.ru",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_adm(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    async with AsyncSession(engine) as db:
        result = await db.execute(select(Admin).where(Admin.username == username))
        admin = result.scalar()
        if admin is None:
            raise credentials_exception
        return admin



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
class AgentApiKeyRequest(BaseModel):
    api_key: str
    name: str | None = None

class AgentRegisterRequest(BaseModel):
    name: str
    desc: str
    email: str

class AdminLoginRequest(BaseModel):
    username: str
    password: str

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await checkalka_redisa()
    for i in range(5):
        asyncio.create_task(worker(i))


@app.post("/api/checks", tags=["Main Reqs"])
async def checkkk(req: CheckRequest, db: AsyncSession = Depends(get_db)):
    if req.type == "full":
        group_id = str(uuid4())

        main_task = Task(
            id=group_id,
            target=req.target,
            type="full",
            port=req.port,
            record_type=None,
            group_id=group_id
        )
        db.add(main_task)

        checks = [
            {"type": "ping"},
            {"type": "http"},
            {"type": "tcp", "port": req.port or 80},
            {"type": "traceroute"},
            {"type": "dns"}
        ]

        sub_task_ids = []
        for ch in checks:
            sub_id = str(uuid4())
            sub_task_ids.append(sub_id)
            new_task = Task(
                id=sub_id,
                target=req.target,
                type=ch["type"],
                port=ch.get("port", req.port),
                record_type=None,
                group_id=group_id
            )
            db.add(new_task)
            task_data = {
                "id": sub_id,
                "group_id": group_id,
                "target": req.target,
                "type": ch["type"],
                "port": ch.get("port", req.port),
                "record_type": None
            }
            await redis_client.lpush("task_queue", json.dumps(task_data))
            print(f"📦 Sub-task {sub_id} added to Redis queue for group {group_id}")

        await db.commit()

        return {"id": group_id, "status": "queued"}
    task_id = str(uuid4())
    new_task = Task(
        id=task_id,
        target=req.target,
        type=req.type,
        port=req.port,
        record_type=req.record_type
    )
    db.add(new_task)
    await db.commit()

    task_data = req.model_dump() | {"id": task_id}
    await dispatch_task(task_data)
    return {"id": task_id, "status": "queued"}


@app.get("/api/checks/{task_id}", tags=["Main Reqs"])
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

    results = await db.execute(select(Result).where(Result.group_id == task_id))
    res_list = results.scalars().all()

    if res_list:
        return {
            "id": task_id,
            "status": "completed" if all(r.status != "pending" for r in res_list) else "pending",
            "results": [
                {
                    "type": r.data.get("type") if r.data else None,
                    "status": r.status,
                    "code": r.code,
                    "response_time": r.response_time,
                    "data": r.data,
                    "error": r.error
                }
                for r in res_list
            ]
        }

    task_query = await db.execute(select(Task).where(Task.id == task_id))
    main_task = task_query.scalar()
    if main_task and main_task.type == "full":
        return {"id": task_id, "status": "pending"}

    return {"id": task_id, "status": "pending"}

@app.delete("/api/agents/{agent_id}", tags=["Admin Reqs"])
async def delete_agent(agent_id: str, db: AsyncSession = Depends(get_db), current_admin: Admin = Depends(get_adm)):
    q = await db.execute(select(Agents).where(Agents.id == agent_id))
    a = q.scalar()
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    await db.delete(a)
    await db.commit()
    q = await db.execute(select(ActiveAgents).where(ActiveAgents.api == a.api))
    aa = q.scalar()
    if aa:
        await db.delete(aa)
        await db.commit()
        if a.api in active_agents:
            ws = active_agents[a.api]
            try:
                await ws.close(code=1000, reason="Agent deleted")
                del active_agents[a.api]
            except:
                del active_agents[a.api]
    return {"message": f"Agent {agent_id} deleted"}

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
                        data = {
                            "headers": dict(r.headers),
                            "url": str(r.url),
                            "type": "http"
                        }
                        status = "ok" if r.status_code < 400 else "fail"
                        db.add(Result(
                            id=task["id"],
                            status=status,
                            code=r.status_code,
                            response_time=time.time()-start,
                            data=data,
                            group_id=task.get("group_id")
                        ))
                        await db.commit()

                elif task["type"] == "ping":
                    proc = await asyncio.create_subprocess_shell(
                        f"/bin/ping -c 1 {task['target']}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE)
                    out, err = await proc.communicate()
                    encoding = locale.getpreferredencoding(False)
                    out_decoded = out.decode(encoding, errors="replace")
                    err_decoded = err.decode(encoding, errors="replace")

                    ok = proc.returncode == 0
                    resp_time = None
                    if ok and "time=" in out_decoded:
                        try:
                            line = out_decoded.split("time=")[1]
                            resp_time = float(line.split(" ")[0])
                        except:
                            pass

                    db.add(Result(
                        id=task["id"],
                        status="ok" if ok else "fail",
                        response_time=resp_time,
                        data={
                            "output": out_decoded,
                            "type": "ping"
                        },
                        error=None if ok else err_decoded,
                        group_id=task.get("group_id")
                    ))
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
                    db.add(Result(
                        id=task["id"],
                        status="ok" if ok else "fail",
                        response_time=time.time()-start,
                        data={
                            "host": host,
                            "port": port,
                            "type": "tcp"
                        },
                        error=err,
                        group_id=task.get("group_id")
                    ))
                    await db.commit()

                elif task["type"] == "traceroute":
                    proc = await asyncio.create_subprocess_shell(
                        f"/bin/traceroute -m 10 -w 2 {task['target']}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE)
                    out, err = await proc.communicate()
                    ok = proc.returncode == 0
                    db.add(Result(
                        id=task["id"],
                        status="ok" if ok else "fail",
                        response_time=time.time()-start,
                        data={
                            "trace": out.decode().splitlines(),
                            "type": "traceroute"
                        },
                        error=None if ok else err.decode(),
                        group_id=task.get("group_id")
                    ))
                    await db.commit()

                elif task["type"] == "dns":
                    resolver = dns.resolver.Resolver()
                    record_types = ["A", "AAAA", "MX", "NS", "TXT", "CNAME"]
                    dns_results = {}
                    for rt in record_types:
                        try:
                            answer = resolver.resolve(task["target"], rt)
                            dns_results[rt] = [rdata.to_text() for rdata in answer]
                        except Exception as e:
                            dns_results[rt] = {"error": str(e)}
                    db.add(Result(
                        id=task["id"],
                        status="ok",
                        response_time=time.time() - start,
                        data={
                            "records": dns_results,
                            "type": "dns"
                        },
                        group_id=task.get("group_id")
                    ))
                    await db.commit()

            except Exception as e:
                db.add(Result(
                    id=task["id"],
                    status="error",
                    error=str(e),
                    data={"type": task["type"]},
                    group_id=task.get("group_id")
                ))
                await db.commit()


@app.post("/api/agents/register", tags=["Agents Req"])
async def reg_ag(req: AgentRegisterRequest, db: AsyncSession = Depends(get_db)):
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
    send_api(req.email, req.name, api_key)

    return {
        "id": agent_id,
        "status": status,
        "api_key": api_key,
        "name": req.name,
        "desc": req.desc,
        "email": req.email
    }



@app.get("/api/agents", tags=["Admin Reqs"])
async def get_agents(db: AsyncSession = Depends(get_db), current_admin: Admin = Depends(get_adm)):
    agents_res = await db.execute(select(Agents))
    agents = agents_res.scalars().all()

    act_agents = await db.execute(select(ActiveAgents))
    aa_list = act_agents.scalars().all()
    act_api_keys = {agent.api for agent in aa_list}

    agents_data = []
    for agent in agents:
        is_active = agent.api in act_api_keys
        agents_data.append({
            "id": agent.id,
            "name": agent.name,
            "desc": agent.desc,
            "email": agent.email,
            "status": "Active" if is_active else "Inactive",
            "api_key": agent.api
        })

    total = len(agents)
    act_cout = len(aa_list)
    inact_cout = total - act_cout

    return {
        "statistics": {
            "total": total,
            "active": act_cout,
            "inactive": inact_cout
        },
        "agents": agents_data
    }


@app.post("/api/login", tags=["Admin Reqs"])
async def login(req: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Admin).where(Admin.username == req.username))
    admin = result.scalar()

    if not admin:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if not pwd_context.verify(req.password, admin.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    expire = datetime.utcnow() + access_token_expires
    to_encode = {"sub": admin.username, "exp": expire}
    access_token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

    return {
        "access_token": access_token,
        "token_type": "bearer"
    }
@app.websocket("/ws/onlineag") #count
async def ag_count(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({"online": len(active_agents)})
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/agent")
async def agent_ws(websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    await websocket.accept()
    api_key = websocket.query_params.get("api_key")

    if not api_key:
        await websocket.close(code=4000)
        return

    agent_query = await db.execute(select(Agents).where(Agents.api == api_key))
    agent = agent_query.scalar()
    if not agent:
        await websocket.close(code=4001)
        return

    client_ip = websocket.client.host
    active_agents[api_key] = websocket
    existing_active = await db.execute(select(ActiveAgents).where(ActiveAgents.api == api_key))
    existing_active = existing_active.scalar()

    if not existing_active:
        new_active = ActiveAgents(
            id=str(uuid4()),
            status="Active",
            name=agent.name,
            ip=client_ip,
            api=api_key
        )
        db.add(new_active)
    else:
        existing_active.status = "Active"
        existing_active.ip = client_ip

    agent.last_ip = client_ip
    await db.commit()

    print(f"🟢 Агент приконекчен: {agent.name} | IP: {client_ip}")

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            if data.get("type") == "result":
                result_data = data["result"]
                print(f"Результат получен от агента {agent.name} | {result_data['status']} | {result_data.get('response_time', 'N/A')}")

                existing = await db.get(Result, result_data["id"])
                if not existing:
                    new_result = Result(
                        id=result_data["id"],
                        status=result_data["status"],
                        code=result_data.get("code"),
                        response_time=result_data.get("response_time"),
                        data=result_data.get("data"),
                        error=result_data.get("error")
                    )
                    db.add(new_result)
                else:
                    existing.status = result_data["status"]
                    existing.code = result_data.get("code")
                    existing.response_time = result_data.get("response_time")
                    existing.data = result_data.get("data")
                    existing.error = result_data.get("error")

                await db.commit()

    except WebSocketDisconnect:
        print(f"🔴 Агент потерялся: {agent.name}")
        result = await db.execute(select(ActiveAgents).where(ActiveAgents.api == api_key))
        active_record = result.scalar()
        if active_record:
            await db.delete(active_record)
            await db.commit()
        if api_key in active_agents:
            del active_agents[api_key]

async def dispatch_task(task_data: dict):
    if active_agents:
        import random
        api_key = random.choice(list(active_agents.keys()))
        ws = active_agents[api_key]
        try:
            await ws.send_text(json.dumps({
                "type": "task",
                "task_id": task_data["id"],
                "data": task_data
            }))
        except Exception as e:
            await redis_client.lpush("task_queue", json.dumps(task_data))
    else:
        await redis_client.lpush("task_queue", json.dumps(task_data))


@app.post("/api/admin/register", tags=["Admin Reqs"])
async def register_admin(req: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Admin).where(Admin.username == req.username))
    if result.scalar():
        raise HTTPException(status_code=400, detail="Username already exists")

    hashed_password = pwd_context.hash(req.password)
    new_admin = Admin(id=str(uuid4()), username=req.username, hashed_password=hashed_password)
    db.add(new_admin)
    await db.commit()
    return {"message": f"Admin {req.username} registered successfully"}



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
