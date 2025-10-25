from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from uuid import uuid4
import asyncio, time, httpx, dns.resolver, json
import redis.asyncio as redis
import secrets
import os

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

@app.middleware("http")
async def ensure_utf8(request, call_next):
    response = await call_next(request)
    if isinstance(response, JSONResponse):
        response.headers["Content-Type"] = "application/json; charset=utf-8"
    elif response.headers.get("content-type", "").startswith("text/"):
        response.headers["Content-Type"] = response.headers["Content-Type"] + "; charset=utf-8"
    return response


active_agents: dict[str, WebSocket] = {}
redis_client = redis.Redis(host='localhost', port=6379, db=0, encoding="utf-8", decode_responses=True)
# redis_client = redis.Redis(host=os.getenv('REDIS_HOST', 'localhost'), port=6379, db=0, encoding="utf-8", decode_responses=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8501",
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
            new_task = Task(id=task_id, target=req.target, type=req.type,
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
            await dispatch_task(task_data)
        return {"id": group_id, "status": "queued", "parts": len(checks)}

    new_task = Task(id=task_id, target=req.target, type=req.type,
                    port=req.port, record_type=req.record_type)
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
                        data = {"headers": dict(r.headers), "url": str(r.url)}
                        status = "ok" if r.status_code < 400 else "fail"
                        db.add(Result(id=task["id"], status=status,
                                      code=r.status_code,
                                      response_time=time.time()-start,
                                      data=data))
                        await db.commit()

                elif task["type"] == "ping":
                    proc = await asyncio.create_subprocess_shell(
                        f"/bin/ping -c 1 {task['target']}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE)
                    out, err = await proc.communicate()
                    try:
                        decoded_out = out.decode("utf-8")
                    except UnicodeDecodeError:
                        decoded_out = out.decode("cp866", errors="replace")
                    try:
                        decoded_err = err.decode("utf-8")
                    except UnicodeDecodeError:
                        decoded_err = err.decode("cp866", errors="replace")
                    ok = proc.returncode == 0
                    resp_time = None
                    if ok and "time=" in decoded_out:
                        try:
                            line = decoded_out.split("time=")[1]
                            resp_time = float(line.split(" ")[0])
                        except:
                            pass
                    db.add(Result(
                        id=task["id"],
                        status="ok" if ok else "fail",
                        response_time=resp_time,
                        data={"output": decoded_out},
                        error=None if ok else decoded_err
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
                    db.add(Result(id=task["id"], status="ok" if ok else "fail",
                                  response_time=time.time()-start,
                                  data={"host": host, "port": port},
                                  error=err))
                    await db.commit()

                elif task["type"] == "traceroute":
                    proc = await asyncio.create_subprocess_shell(
                        f"/bin/traceroute -m 10 -w 2 {task['target']}",
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


@app.post("/api/agents/activate", tags=["Agents Req"])
async def activate_agent(req: AgentApiKeyRequest, db: AsyncSession = Depends(get_db)):
    agent = await db.execute(select(Agents).where(Agents.api == req.api_key))
    agent = agent.scalar()

    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API key")

    existing_active = await db.execute(
        select(ActiveAgents).where(ActiveAgents.api == req.api_key)
    )
    if existing_active.scalar():
        raise HTTPException(status_code=400, detail="Agent already activated")

    active_agent_id = str(uuid4())
    new_active_agent = ActiveAgents(
        id=active_agent_id,
        status="Active",
        name=req.name or agent.name,
        api=req.ap
    )

    db.add(new_active_agent)
    await db.commit()

    return {
        "id": active_agent_id,
        "status": "Active",
        "name": new_active_agent.name,
        "api_key": req.api_key,
        "message": "Agent successfully activated"
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
@app.websocket("/ws/agents/count")
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

    active_agents[api_key] = websocket
    existing_active = await db.execute(
        select(ActiveAgents).where(ActiveAgents.api == api_key)
    )
    existing_active = existing_active.scalar()

    if not existing_active:
        new_active = ActiveAgents(
            id=str(uuid4()),
            status="Active",
            name=agent.name,
            api=api_key
        )
        db.add(new_active)
        await db.commit()

    print(f"🟢 Агент приконекчен: {agent.name}")

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            if data.get("type") == "result":
                result_data = data["result"]

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
                    await db.commit()
                else:
                    existing.status = result_data["status"]
                    existing.code = result_data.get("code")
                    existing.response_time = result_data.get("response_time")
                    existing.data = result_data.get("data")
                    existing.error = result_data.get("error")
                    await db.commit()

                print(f"✅ Результат получен от агента {agent.name} для задачи {result_data['id']}")


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
    await redis_client.lpush("task_queue", json.dumps(task_data))
    print(f"📦 Task {task_data['id']} added to Redis queue")
    if active_agents:
        for api_key, ws in active_agents.items():
            try:
                await ws.send_text(json.dumps({
                    "type": "task",
                    "task_id": task_data["id"],
                    "data": task_data
                }))
                print(f"📤 Task {task_data['id']} sent to agent {api_key}")
            except Exception as e:
                print(f"⚠️ Failed to send task to agent {api_key}: {e}")
    else:
        print("⚠️ No active agents connected — task will be processed locally by worker")


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
