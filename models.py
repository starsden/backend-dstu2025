from sqlalchemy import Column, String, Float, JSON, Text
from database import Base

class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    target = Column(String, nullable=False)
    type = Column(String, nullable=False)
    port = Column(Float, nullable=True)
    record_type = Column(String, nullable=True)
    group_id = Column(String, nullable=True)


class Result(Base):
    __tablename__ = "results"

    id = Column(String, primary_key=True)
    status = Column(String)
    code = Column(Float, nullable=True)
    response_time = Column(Float, nullable=True)
    data = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    group_id = Column(String, nullable=True, index=True)

class Agents(Base):
    __tablename__ = "agents"

    id = Column(String, primary_key=True)
    status = Column(String)
    name = Column(String, nullable=True)
    desc = Column(String, nullable=True)
    email = Column(String, nullable=True)
    last_ip = Column(String, nullable=True)
    api = Column(String, nullable=True)

class ActiveAgents(Base):
    __tablename__ = "active_agents"

    id = Column(String, primary_key=True)
    status = Column(String)
    name = Column(String, nullable=True)
    ip = Column(String, nullable=True)
    api = Column(String, nullable=True)

class Admin(Base):
    __tablename__ = "admins"
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    username = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)



