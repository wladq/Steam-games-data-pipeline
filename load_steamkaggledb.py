import getpass
import pandas as pd
from sqlalchemy import create_engine, text, URL

CSV = r"C:\Users\you\Downloads\steam\games.csv"   # <- your path

# --- connect (through the tunnel) ---
url = URL.create(
    "postgresql+psycopg",
    username="wlad",
    password=getpass.getpass("DB password: "),   # asks, so it's never saved in the file
    host="localhost",
    port=5433,
    database="steam",
)
engine = create_engine(url)

with engine.connect() as conn:                       # quick connection test
    print(conn.execute(text("SELECT version()")).scalar())

# --- read the CSV and fix the shifted header ---
cols = pd.read_csv(CSV, nrows=0).columns.tolist()
i = cols.index("DiscountDLC count")
fixed = cols[:i] + ["Discount", "DLC count"] + cols[i + 1:]
df = pd.read_csv(CSV, header=0, names=fixed, index_col=False)

# --- tidy the column names: "Estimated owners" -> estimated_owners ---
df.columns = (df.columns.str.strip().str.lower()
              .str.replace(r"[^a-z0-9]+", "_", regex=True)
              .str.strip("_"))

# --- sanity checks: stop here if anything is still shifted ---
assert pd.to_numeric(df["appid"], errors="coerce").notna().all()
assert df["discount"].between(0, 100).all()
print(df.shape)

# --- upload ---
df.to_sql("games_raw", engine, if_exists="replace", index=False,
          chunksize=1000, method="multi")

# --- primary key: also a duplicate check, since it fails if any appid repeats ---
with engine.begin() as conn:
    conn.execute(text("ALTER TABLE games_raw ADD PRIMARY KEY (appid)"))
print("Done")