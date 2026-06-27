"""Quick pipeline test — 3 NSE stocks"""
import sys
sys.path.insert(0, 'scripts')
from fetch_data import process_batch, YF_SESSION, _session_label

print("Session:", _session_label)
print("Testing batch download for RELIANCE, TCS, INFY...")

result = process_batch(
    ['RELIANCE', 'TCS', 'INFY'],
    ['RELIANCE.NS', 'TCS.NS', 'INFY.NS']
)

if not result:
    print("ERROR: No data returned! Check internet connection.")
else:
    for sym, d in result.items():
        print(f"  {sym}: price=Rs.{d['price']}  chg={d['change_pct']}%  ema20={d['ema20']}  ema50={d['ema50']}  3m={d['perf_3m']}%")
    print(f"\nPIPELINE TEST PASSED - {len(result)}/3 stocks fetched successfully!")
    print("Safe to run full fetch: python scripts\\fetch_data.py")
