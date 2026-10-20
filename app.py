"""LocalLedger OCR — Streamlit entry point.

    streamlit run app.py
"""
from __future__ import annotations

import logging

import streamlit as st

from localledger import __version__

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

st.set_page_config(page_title="LocalLedger OCR", page_icon="📒", layout="wide",
                   menu_items={"About": f"LocalLedger OCR {__version__} — local, private invoice processing."})

pages = {
    "Workflow": [
        st.Page("ui/staging.py", title="Upload & Review", icon="📥", default=True),
        st.Page("ui/history.py", title="History", icon="🗂️"),
    ],
    "Insights": [
        st.Page("ui/analytics.py", title="Analytics", icon="📊"),
    ],
    "Setup": [
        st.Page("ui/entities.py", title="Clients & Suppliers", icon="📇"),
        st.Page("ui/templates.py", title="Export Templates", icon="🧩"),
        st.Page("ui/rules_editor.py", title="Description Rules", icon="✍️"),
        st.Page("ui/settings.py", title="Settings", icon="⚙️"),
    ],
}

with st.sidebar:
    st.markdown("### 📒 LocalLedger OCR")
    st.caption("100% local · your data never leaves this computer")

st.navigation(pages).run()
