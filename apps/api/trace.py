import os
import sys
from sqlalchemy import create_engine, text

def main():
    engine = create_engine("postgresql+psycopg://axon:axon@localhost:5434/axon")
    
    with engine.connect() as conn:
        # Get repo
        repo_res = conn.execute(text("SELECT id, full_name, ingest_status FROM repos WHERE full_name = 'CodeVishal-17/axon-demo'")).fetchone()
        if not repo_res:
            print("Repository not found in DB.")
            return
            
        repo_id, full_name, ingest_status = repo_res
        print(f"Repo: {full_name} | Status: {ingest_status}")
        
        # 1. section extraction (Entities of kind doc_section or similar)
        entities_count = conn.execute(text("SELECT COUNT(*) FROM entities WHERE repo_id = :repo_id"), {"repo_id": repo_id}).scalar()
        print(f"Entities: {entities_count}")
        
        # 2. claim extraction
        claims_count = conn.execute(text("SELECT COUNT(*) FROM claims WHERE repo_id = :repo_id"), {"repo_id": repo_id}).scalar()
        print(f"Claims: {claims_count}")
        
        # 3. embedding retrieval (claim links?)
        links_count = conn.execute(text("SELECT COUNT(*) FROM claim_links cl JOIN claims c ON c.id = cl.claim_id WHERE c.repo_id = :repo_id"), {"repo_id": repo_id}).scalar()
        print(f"Claim Links: {links_count}")

        # verification / events / jobs
        jobs = conn.execute(text("SELECT kind, status, error FROM jobs ORDER BY created_at DESC LIMIT 10")).fetchall()
        print("\nRecent Jobs:")
        for j in jobs:
            print(f"- {j.kind} | {j.status} | {j.error}")
            
        events = conn.execute(text("SELECT kind, id FROM events WHERE repo_id = :repo_id"), {"repo_id": repo_id}).fetchall()
        print(f"Events: {len(events)}")
        
        findings_count = conn.execute(text("SELECT COUNT(*) FROM findings WHERE repo_id = :repo_id"), {"repo_id": repo_id}).scalar()
        print(f"Findings: {findings_count}")

if __name__ == "__main__":
    main()
