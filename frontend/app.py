import streamlit as st
import requests
import time
import pandas as pd
import re

apishka = "http://127.0.0.1:8000/api"

st.set_page_config(page_title="DNS чекалка", page_icon="🦦", layout="centered")
st.title("🦦 Че-че-че")
st.markdown("Проверка доступности хостов и DNS")

target = st.text_input("Введите домен или IP", "google.com")

ct = st.selectbox(
    "Тип проверки",
    ["http", "ping", "tcp", "traceroute", "dns", "full"]
)

port = None
recty = None

if ct == "tcp":
    port = st.number_input("Порт", value=80, step=1)

if ct == "dns":
    recty = st.selectbox("Тип DNS-записи", ["A", "AAAA", "MX", "NS", "TXT"])

if st.button("🔍 Запустить проверку"):
    with st.spinner("Отправляем запрос..."):
        data = {"target": target, "type": ct}
        if port:
            data["port"] = port
        if recty:
            data["record_type"] = recty

        res = requests.post(f"{apishka}/checks", json=data)

        if res.status_code == 200:
            task = res.json()
            task_id = task["id"]
            st.success(f"✅ Задача создана! ID: `{task_id}`")

            with st.spinner("Выполняется проверка..."):
                for _ in range(60):
                    time.sleep(1)
                    result = requests.get(f"{apishka}/checks/{task_id}").json()


                    if ct == "full":
                        if "results" in result and any(r["status"] not in ("pending", "queued") for r in result["results"]):
                            st.subheader("📊 Результаты полной проверки:")
                            for r in result["results"]:
                                st.markdown(f"### 🔹 {r['type'].upper()} — {r['status'].upper()}")
                                st.write(f"⏱ Время отклика: {r.get('response_time', '—')} сек")

                                # HTTP
                                if r["type"] == "http" and r.get("data"):
                                    headers = r["data"].get("headers", {})
                                    st.subheader("🔎 HTTP-заголовки")
                                    st.table(pd.DataFrame(list(headers.items()), columns=["Header", "Value"]))

                                # DNS
                                if r["type"] == "dns" and r.get("data"):
                                    records = r["data"].get("records", [])
                                    st.subheader(f"🌐 DNS-записи ({r['data'].get('type')})")
                                    st.table(pd.DataFrame(records, columns=["Record"]))

                                # Traceroute
                                if r["type"] == "traceroute" and r.get("data"):
                                    trace_lines = r["data"].get("trace", [])
                                    hops = []
                                    for line in trace_lines:
                                        m = re.findall(
                                            r"(\d+)\s+([\w\.\-]+)\s+\(([\d\.]+)\).*?(\d+\.\d+)\s+ms", line
                                        )
                                        if m:
                                            hop_num, host, ip, delay = m[0]
                                            hops.append({
                                                "Hop": int(hop_num),
                                                "Host": host,
                                                "IP": ip,
                                                "Delay (ms)": float(delay)
                                            })
                                    if hops:
                                        df = pd.DataFrame(hops)
                                        st.subheader("🧭 Маршрут до хоста")
                                        st.dataframe(df)
                                        st.line_chart(df.set_index("Hop")["Delay (ms)"])
                            break
                    else:
                        if result.get("status") not in ("pending", "queued"):
                            st.subheader("📊 Результат проверки:")
                            st.json(result)

                            status = result.get("status")
                            rt = result.get("response_time")
                            st.write(f"**Статус:** {status}")
                            if rt:
                                st.metric("⏱ Время отклика (сек)", round(rt, 3))

                            if ct == "http" and result.get("data"):
                                st.subheader("🔎 HTTP-заголовки")
                                headers = result["data"].get("headers", {})
                                st.table(pd.DataFrame(list(headers.items()), columns=["Header", "Value"]))

                            if ct == "traceroute" and result.get("data"):
                                trace_lines = result["data"].get("trace", [])
                                hops = []
                                for line in trace_lines:
                                    m = re.findall(
                                        r"(\d+)\s+([\w\.\-]+)\s+\(([\d\.]+)\).*?(\d+\.\d+)\s+ms", line
                                    )
                                    if m:
                                        hop_num, host, ip, delay = m[0]
                                        hops.append({
                                            "Hop": int(hop_num),
                                            "Host": host,
                                            "IP": ip,
                                            "Delay (ms)": float(delay)
                                        })
                                if hops:
                                    df = pd.DataFrame(hops)
                                    st.subheader("🧭 Маршрут до хоста")
                                    st.dataframe(df)
                                    st.line_chart(df.set_index("Hop")["Delay (ms)"])

                            # DNS
                            if ct == "dns" and result.get("data"):
                                records = result["data"].get("records", [])
                                st.subheader(f"🌐 DNS-записи ({result['data'].get('type')})")
                                st.table(pd.DataFrame(records, columns=["Record"]))

                            break
                else:
                    st.warning("⏳ Проверка заняла слишком много времени.")
        else:
            st.error(f"Ошибка API: {res.text}")
