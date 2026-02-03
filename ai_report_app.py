import streamlit as st
import pandas as pd
import re
from openai import AzureOpenAI
from sqlalchemy import create_engine, text
import os
import json
import pyodbc
import openai
from sqlalchemy.pool import NullPool

def get_table_columns(conn, table_name):
    query = f"""
    SELECT COLUMN_NAME 
    FROM INFORMATION_SCHEMA.COLUMNS 
    WHERE TABLE_SCHEMA = PARSENAME('{table_name}', 2) 
      AND TABLE_NAME = PARSENAME('{table_name}', 1)
    ORDER BY ORDINAL_POSITION
    """
    df = pd.read_sql(query, conn)
    return df['COLUMN_NAME'].tolist()

#File to store query history
HISTORY_FILE = "query_history.json"

#Load history from file if exists
if "query_history" not in st.session_state:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            st.session_state.query_history = json.load(f)
    else:
        st.session_state.query_history = []

#Initialize Azure OpenAI client
client = AzureOpenAI(
    api_key="YOUR API KEY HERE",
    api_version="2025-01-01-preview",
    azure_endpoint="https://aitooltest.openai.azure.com/",
)


st.set_page_config(layout="wide")
st.title("Dynamic AI Report Generator")

#Initialize session state variables
if "engine" not in st.session_state:
    st.session_state.engine = None
if "conn" not in st.session_state:
    st.session_state.conn = None
if "table_names" not in st.session_state:
    st.session_state.table_names = []
if "db_user" not in st.session_state:
    st.session_state.db_user = ""
if "prompt_input" not in st.session_state:
    st.session_state.prompt_input = ""
if "chat_history" not in st.session_state:
    st.session_state.chat_history=[]  
if "table_columns" not in st.session_state:
    st.session_state.table_columns = {}
if "model_deployment_name" not in st.session_state:
    st.session_state.model_deployment_name = "gpt-4o"
if "assistant_greeted" not in st.session_state:
    st.session_state.assistant_greeted = False
if "trigger_followup" not in st.session_state:
   st.session_state.trigger_follow_up = False
if "followup_input_text" not in st.session_state:
    st.session_state.followup_input_text = ""


user_input = st.session_state.get("prompt_input","")


#Sidebar: Database connection & history
with st.sidebar:
    st.header("Database Connection")

    server = st.text_input("SQL Server Name")
    database = st.text_input("Database Name")
    use_trusted = st.checkbox("Use Windows Authentication", value=True)

    if not use_trusted:
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
    else:
        username = password = ""

    if st.button("Connect to Database"):
        try:
            if st.session_state.conn:
                st.session_state.conn.close()
            if use_trusted:
                conn_str = (
                    f"Driver={{ODBC Driver 17 for SQL Server}};"
                    f"Server={server};Database={database};"
                    "Trusted_Connection=yes;"
                    "TrustServerCertificate=yes;"
                    "MARS_Connection=yes;"
                )
            else:
                conn_str = (
                    f"Driver={{ODBC Driver 17 for SQL Server}};"
                    f"Server={server};Database={database};"
                    f"UID={username};PWD={password};"
                      "TrustServerCertificate=yes;"
                      "MARS_Connection=yes;"
                     )

            engine = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")
            st.session_state.conn = engine.connect()
            st.session_state.engine = engine
            with st.session_state.conn.begin():
             result = st.session_state.conn.execute(text("SELECT CURRENT_USER;"))
             st.session_state.db_user = result.fetchone()[0]

            tables_df = pd.read_sql("""
                SELECT QUOTENAME(s.name) + '.' + QUOTENAME(t.name) AS FullTableName
                FROM sys.tables t
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                ORDER BY FullTableName
            """, st.session_state.conn)
            st.session_state.table_names = tables_df["FullTableName"].tolist()
            st.success(f"Connected as user: {st.session_state.db_user}")

        except Exception as e:
            st.error(f"Connection error: {e}")
            st.session_state.conn = None
            st.session_state.table_names = []
            st.session_state.db_user = ""

    # New Chat button — clears prompt input
    if st.session_state.conn:
        if st.button("New Chat"):
            st.session_state.prompt_input = ""
            st.session_state.chat_history=[]

        st.markdown("### Query History")

    # Query History as clickable text inside an expander
    with st.expander("Query History"):
        if st.session_state.query_history:
         for i, query in enumerate(reversed(st.session_state.query_history[-10:])):
                query_key = f"history_click_{i}"
                clicked = st.checkbox(f"{query}", key=query_key, value=False)
                if clicked:
                    st.session_state.prompt_input = query
                    st.session_state.trigger_follow_up=True
                    # Reset others
                    for j in range(len(st.session_state.query_history[-10:])):
                        if j != i:
                            st.session_state[f"history_click_{j}"] = False
        else:
            st.write("No query history yet.")

#Main UI Section
if st.session_state.conn:
    col1, col2 = st.columns([1, 14])

    with col2:
        st.markdown("### AI-Powered SQL Query Generator")

        #Display chat history 
        if "chat_history" in st.session_state and st.session_state.chat_history:
       #Display chat at the bottom of the page
         chat_container = st.container()

         with chat_container:
          st.markdown("")  # Spacer to push chat lower

        st.markdown('<div id="scroll_target"></div>', unsafe_allow_html=True)
        #Prepare options with a placeholder first
        table_options = ["-- Select a table --"] + st.session_state.table_names

        #Show selectbox with placeholder
        selected_table = st.selectbox(
         "Select a table",
         table_options,
         key="table_select"
         )

        #Treat the placeholder as no selection
        if selected_table == "-- Select a table --":
         selected_table = None
        if selected_table:
         table_schema, table_name = selected_table.replace("[","").replace("]","").split(".")
         column_query = f"""
          SELECT COLUMN_NAME 
          FROM INFORMATION_SCHEMA.COLUMNS 
          WHERE TABLE_SCHEMA = '{table_schema}' AND TABLE_NAME = '{table_name}'
         """
         columns_df = pd.read_sql(column_query, st.session_state.conn)
         table_columns = columns_df["COLUMN_NAME"].tolist()
         st.session_state["table_columns"] = table_columns

         if st.checkbox("Show table columns"):
          st.markdown("*Columns in selected table:*")
          st.table(pd.DataFrame(table_columns, columns=["Column Name"]))

          # After columns are fetched and stored
         if not st.session_state.assistant_greeted:
          greeting = f"Hello! You're now exploring *{selected_table}*. How may I assist you with this table?"
          st.session_state.chat_history.append({"role": "assistant", "content": greeting})
          st.session_state.assistant_greeted = True
         

         if selected_table:
          column_query = f"""
           SELECT COLUMN_NAME
           FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_SCHEMA = '{selected_table.split('.')[0].strip('[]')}'
            AND TABLE_NAME = '{selected_table.split('.')[1].strip('[]')}'
           """
          columns_df = pd.read_sql(column_query, st.session_state.conn)
          table_columns = columns_df["COLUMN_NAME"].tolist()
          st.session_state["table_columns"] = table_columns
        # Scrollable Chat Container
        with st.container():
         st.markdown(
            """
            <div style='height:75px; overflow-y: auto; padding-right:10px;'>
            """,
            unsafe_allow_html=True,
        )

        if "prompt_input" in st.session_state and st.session_state.prompt_input:
         user_input = st.session_state.prompt_input

        # Add user input to chat history
        st.session_state.chat_history.append({"role": "user", "content": user_input})

        # Send to model
        response = client.chat.completions.create(
            model=st.session_state.model_deployment_name,
            messages=st.session_state.chat_history,
            temperature=0.2
        )
        # xtract assistant reply
        assistant_reply = response.choices[0].message.content.strip()

         #Append to chat history
        st.session_state.chat_history.append({"role": "assistant", "content": assistant_reply})

         #Clear prompt_input after handling
        st.session_state.prompt_input = ""

         #Display chat history with styled messages
        for message in st.session_state.chat_history:
         role = message.get("role", "")
         content = message.get("content", "")
        if role == "user":
         st.markdown(f"""
            <div style='background-color:#4CAF50; color:white; padding:10px 15px; border-radius:15px; margin:5px 0; max-width:80%; float:right; clear:both;'>
                {content}
            </div>
        """, unsafe_allow_html=True)
        elif role == "assistant":
         st.markdown(f"""
            <div style='background-color:#1C1C1C; color:white; padding:10px 15px; border-radius:15px; margin:5px 0; max-width:80%; float:left; clear:both;'>
                {content}
            </div>
        """, unsafe_allow_html=True)

        #User input area for questions
         user_input = st.text_area(
         "Ask a question about the selected table:",
         height=200,
         key="user_input"
        )

#Initialize run_report flag
run_report = False
if st.session_state.get("trigger_followup", False):
    run_report = True
    st.session_state.trigger_followup = False

if st.button("Generate Report"):
    run_report = True
    st.session_state.prompt_input = st.session_state.get("user_input", "")

if run_report:
    user_input = st.session_state.get("prompt_input", "")
    # DEBUG: Check user input and run_report status
    st.write("DEBUG: user_input =", user_input)
    st.write("DEBUG: run_report =", run_report)
    try:
        deployment_name = st.session_state.model_deployment_name
        columns_str = ",".join(st.session_state.table_columns) if st.session_state.table_columns else "No columns found"
        system_prompt = (
            f"You are a helpful assistant. The user has selected the table '{selected_table}' "
            f"from the database '{database}'. The columns in this table are: {columns_str}. "
            "Based on the user query, generate a valid SQL Server SELECT query using only the relevant columns. "
            "Then provide a brief explanation in plain English on the explanation area. "
            "Ensure the SQL query comes first. "
            "Do a follow up by asking the user a question on what else they might want to know concerning the selected table."
            "When the user responds,generate a report based on what they asked"
        )

        # Add user message to history
        st.session_state.chat_history.append({"role": "user", "content": user_input})

        # Prepare full message history
        messages = [{"role": "system", "content": system_prompt}] + st.session_state.chat_history

        # Send request with full history
        response = client.chat.completions.create(
            model=deployment_name,
            messages=messages
        )

        # Append assistant reply
        assistant_reply = response.choices[0].message.content.strip()
        st.session_state.chat_history.append({"role": "assistant", "content": assistant_reply})

        # Extract SQL query
        sql_match = re.search(r"(SELECT.*?;)", assistant_reply, re.IGNORECASE | re.DOTALL)
        sql_query = sql_match.group(1).strip() if sql_match else None

        if sql_query:
            st.markdown("Generated SQL Query:")
            st.code(sql_query, language="sql")

            # Remove SQL from assistant reply to get explanation + follow-up
            remainder = assistant_reply.replace(sql_query, "").strip()

            # Split remainder into explanation and follow-up (last line as follow-up)
            lines = remainder.splitlines()
            if len(lines) > 1:
                explanation = "\n".join(lines[:-1]).strip()
                follow_up = lines[-1].strip()
                st.session_state["follow_up_question"] = follow_up
            else:
                explanation = remainder
                follow_up = None
            

            # Clean explanation from "undefined" if any
            if explanation.lower().startswith("undefined"):
                explanation = explanation[len("undefined"):].strip()

            if explanation:
                st.markdown("### Explanation:")
                st.write(explanation)

        else:
            st.error("Could not extract a valid SQL SELECT statement from the response.")

    except Exception as e:
        st.error(f"Error generating or executing SQL: {e}")
        st.write("DEBUG: Exception caught in run_report block")
        st.write(e)

    #Show follow-up question from assistant
    if follow_up:
     st.markdown("---")
    st.markdown(f"💬 Assistant: {follow_up}")

    # Input box for user reply to follow-up
    followup_input = st.text_input("Your reply:", key="followup_input_text")

    if st.button("Submit Follow-up", key="submit_followup_btn"):
     st.write("DEBUG: Submit Follow-up clicked")
    followup_input_val = followup_input.strip()
    st.write("DEBUG: followup_input_val =", followup_input_val)
    if followup_input_val:
        # Your logic to append follow-up and rerun
        st.session_state.chat_history.append({"role": "user", "content": followup_input_val})
        st.session_state.prompt_input = followup_input_val
        st.session_state.trigger_followup = True
        st.write("DEBUG: About to rerun with trigger_followup=True")
        st.experimental_rerun()
    else:
        st.warning("Please enter a reply before submitting.")
    # Add current user query to history
    st.session_state.query_history.append(user_input)

    # Execute the SQL safely and show dataframe
    if sql_query and sql_query.strip().lower().startswith("select"):
        with st.session_state.engine.connect() as conn:
            df = pd.read_sql(sql_query, conn)
            st.dataframe(df)
    else:
        st.error("Only SELECT statements are allowed.")

    # Scroll to Top button
    if st.button("🔝 Scroll to Top"):
        st.markdown(
            """
            <script>
            const target = window.parent.document.getElementById('scroll_target');
            if (target) {
                target.scrollIntoView({behavior: 'smooth'});
            }
            </script>
            """,
            unsafe_allow_html=True,
        )

    else:
         st.error("Could not extract a valid SQL SELECT statement from the response.")
          