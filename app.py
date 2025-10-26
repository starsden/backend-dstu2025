import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from database import Base
from models import Task, Result, Agents, ActiveAgents, Admin

st.set_page_config(page_title="Database Viewer", layout="wide")

DATABASE_URL = "sqlite:///./data.db"
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


def fetch_data(model):
    try:
        with SessionLocal() as session:
            result = session.execute(select(model))
            data = result.scalars().all()
        return data
    except Exception as e:
        st.error(f"Ошибка получения данных для {model.__name__}: {str(e)}")
        return []

def to_dataframe(data):
    if not data:
        return pd.DataFrame()
    return pd.DataFrame([{
        column.name: getattr(item, column.name)
        for column in item.__table__.columns
    } for item in data])

st.title("Database Viewer")

if st.button("Обновить данные"):
    st.rerun()

tabs = st.tabs(["Задачи", "Результаты", "Агенты", "Активные агенты", "Админы"])

page_size = 10
page = st.sidebar.number_input("Страница", min_value=1, value=1, step=1)

with tabs[0]:
    st.header("Задачи")
    tasks = fetch_data(Task)
    if tasks:
        df = to_dataframe(tasks)
        type_filter = st.multiselect("Фильтр по типу", options=df["type"].unique(), key="tasks_type")
        if type_filter:
            df = df[df["type"].isin(type_filter)]
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        st.dataframe(df.iloc[start_idx:end_idx], width="stretch")
        st.write(f"Показано {start_idx + 1}–{min(end_idx, len(df))} из {len(df)} задач")
    else:
        st.write("Задачи не найдены.")

with tabs[1]:
    st.header("Результаты")
    results = fetch_data(Result)
    if results:
        df = to_dataframe(results)
        status_filter = st.multiselect("Фильтр по статусу", options=df["status"].unique(), key="results_status")
        if status_filter:
            df = df[df["status"].isin(status_filter)]
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        st.dataframe(df.iloc[start_idx:end_idx], width="stretch")
        st.write(f"Показано {start_idx + 1}–{min(end_idx, len(df))} из {len(df)} результатов")
    else:
        st.write("Результаты не найдены.")

with tabs[2]:
    st.header("Агенты")
    agents = fetch_data(Agents)
    if agents:
        df = to_dataframe(agents)
        status_filter = st.multiselect("Фильтр по статусу", options=df["status"].unique(), key="agents_status")
        if status_filter:
            df = df[df["status"].isin(status_filter)]
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        st.dataframe(df.iloc[start_idx:end_idx], width="stretch")
        st.write(f"Показано {start_idx + 1}–{min(end_idx, len(df))} из {len(df)} агентов")
    else:
        st.write("Агенты не найдены.")

with tabs[3]:
    st.header("Активные агенты")
    active_agents = fetch_data(ActiveAgents)
    if active_agents:
        df = to_dataframe(active_agents)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        st.dataframe(df.iloc[start_idx:end_idx], width="stretch")
        st.write(f"Показано {start_idx + 1}–{min(end_idx, len(df))} из {len(df)} активных агентов")
    else:
        st.write("Активные агенты не найдены.")

with tabs[4]:
    st.header("Админы")
    admins = fetch_data(Admin)
    if admins:
        df = to_dataframe(admins).drop(columns=["hashed_password"], errors="ignore")
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        st.dataframe(df.iloc[start_idx:end_idx], width="stretch")
        st.write(f"Показано {start_idx + 1}–{min(end_idx, len(df))} из {len(df)} админов")
    else:
        st.write("Админы не найдены.")