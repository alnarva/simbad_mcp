import os
import json
import sqlite3
import urllib.request
import glob
from datetime import datetime

DB_PATH = "/home/alnar/Projects/MCP_SIMBAD/db/simbad_chats.db"
OLLAMA_URL = "http://localhost:11434/api/embeddings"

def get_ollama_embedding(text):
    data = {
        "model": "spike",
        "prompt": f"search_document: {text}"
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            res = json.loads(response.read().decode("utf-8"))
            return res.get("embedding")
    except Exception:
        return None

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            title TEXT,
            content TEXT,
            embedding TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

def record_exists(cursor, url, content):
    cursor.execute("SELECT id FROM chats_context WHERE url = ? AND content = ?", (url, content))
    return cursor.fetchone() is not None

def insert_record(cursor, url, title, content):
    if record_exists(cursor, url, content):
        return False
    embedding = get_ollama_embedding(content)
    if not embedding:
        return False
    embedding_json = json.dumps(embedding)
    cursor.execute("""
        INSERT INTO chats_context (url, title, content, embedding, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (url, title, content, embedding_json, datetime.now().isoformat()))
    return True

def sync_claude_code():
    claude_history_path = os.path.expanduser("~/.claude/history.jsonl")
    if not os.path.exists(claude_history_path):
        print("  Claude Code history file not found. Skipping.")
        return
        
    print("  Syncing Claude Code history...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    added_count = 0
    
    try:
        with open(claude_history_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    query = data.get("display")
                    project = data.get("project", "Unknown Project")
                    ts = data.get("timestamp")
                    if query and query.strip():
                        content = f"Project: {project}\nUser Query: {query}"
                        url = f"claude-code://history?ts={ts}"
                        title = f"Claude Code Query in {os.path.basename(project)}"
                        if insert_record(cursor, url, title, content):
                            added_count += 1
                except Exception:
                    continue
        conn.commit()
    except Exception as e:
        print(f"  Error syncing Claude Code: {e}")
    finally:
        conn.close()
    print(f"  ✓ Added {added_count} new Claude Code queries to Simbad memory.")

def sync_antigravity():
    brain_dir = os.path.expanduser("~/.gemini/antigravity-cli/brain")
    if not os.path.exists(brain_dir):
        print("  Antigravity CLI directory not found. Skipping.")
        return
        
    print("  Syncing Antigravity CLI transcripts...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    added_count = 0
    
    # Find all transcript.jsonl files inside hidden .system_generated logs folders
    pattern = os.path.join(brain_dir, "**/.system_generated/logs/transcript.jsonl")
    files = glob.glob(pattern, recursive=True)
    
    for filepath in files:
        parts = filepath.split(os.sep)
        conv_id = "unknown"
        try:
            conv_id = parts[-5]
        except Exception:
            pass
            
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        step = json.loads(line)
                        step_type = step.get("type")
                        step_idx = step.get("step_index", 0)
                        
                        if step_type in ["USER_INPUT", "PLANNER_RESPONSE"]:
                            raw_content = step.get("content", "")
                            if not raw_content or not raw_content.strip():
                                continue
                            
                            source = "User" if step_type == "USER_INPUT" else "AI"
                            content = f"CLI Conversation: {conv_id}\nRole: {source}\nMessage: {raw_content}"
                            url = f"antigravity://transcript/{conv_id}/{step_idx}?role={source}"
                            title = f"Antigravity Chat (Step {step_idx})"
                            
                            if insert_record(cursor, url, title, content):
                                added_count += 1
                    except Exception:
                        continue
        except Exception:
            continue
            
    conn.commit()
    conn.close()
    print(f"  ✓ Added {added_count} new Antigravity steps to Simbad memory.")

def sync_opencode():
    opencode_db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if not os.path.exists(opencode_db_path):
        print("  OpenCode database file not found. Skipping.")
        return
        
    print("  Syncing OpenCode history...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    added_count = 0
    
    try:
        opencode_conn = sqlite3.connect(opencode_db_path)
        opencode_cursor = opencode_conn.cursor()
        opencode_cursor.execute("""
            SELECT 
                s.id,
                s.directory,
                s.title,
                json_extract(m.data, '$.role'),
                p.id,
                json_extract(p.data, '$.text'),
                p.time_created
            FROM part p
            JOIN message m ON p.message_id = m.id
            JOIN session s ON p.session_id = s.id
            WHERE json_extract(p.data, '$.type') = 'text'
        """)
        rows = opencode_cursor.fetchall()
        opencode_conn.close()
        
        for row in rows:
            session_id, directory, title, role, part_id, text, time_created = row
            if not text or not text.strip():
                continue
                
            role_label = "User" if role == "user" else "AI"
            content = f"Source: OpenCode\nProject: {directory}\nSession: {title}\nRole: {role_label}\nMessage: {text}"
            url = f"opencode://session/{session_id}/{part_id}?role={role_label}"
            title_str = f"OpenCode Chat in {title or 'Untitled Session'}"
            
            if insert_record(cursor, url, title_str, content):
                added_count += 1
        conn.commit()
    except Exception as e:
        print(f"  Error syncing OpenCode: {e}")
    finally:
        conn.close()
    print(f"  ✓ Added {added_count} new OpenCode steps to Simbad memory.")

def auto_register_mcp():
    print("🔌 Checking MCP registrations in local CLIs...")
    
    # Shared-skills for all CLIs
    agents_skills_path = os.path.expanduser("~/.agents/skills")
    shared_skills_path = os.path.expanduser("~/.local/share/shared-skills")
    if os.path.isdir(shared_skills_path):
        if os.path.islink(agents_skills_path) and os.readlink(agents_skills_path) == shared_skills_path:
            print("  ✓ Antigravity already connected to shared-skills.")
        else:
            if os.path.islink(agents_skills_path) or os.path.isdir(agents_skills_path):
                import shutil
                if os.path.islink(agents_skills_path):
                    os.remove(agents_skills_path)
                else:
                    shutil.rmtree(agents_skills_path)
            os.symlink(shared_skills_path, agents_skills_path)
            print(f"  ✓ Antigravity connected to shared-skills ({agents_skills_path} → {shared_skills_path}).")

    # 1. OpenCode
    opencode_config = os.path.expanduser("~/.config/opencode/opencode.json")
    if os.path.exists(opencode_config):
        try:
            with open(opencode_config, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        
        mcp = data.setdefault("mcp", {})
        if "simbad-bridge" not in mcp:
            print("  Registering Simbad in OpenCode...")
            mcp["simbad-bridge"] = {
                "enabled": True,
                "type": "local",
                "command": [
                    "docker",
                    "run",
                    "-i",
                    "--rm",
                    "--network=host",
                    "-v",
                    "/home/alnar/Projects/MCP_SIMBAD/db:/app/db",
                    "simbad"
                ]
            }
            try:
                with open(opencode_config, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                print("  ✓ Registered in OpenCode.")
            except Exception as e:
                print(f"  Error registering in OpenCode: {e}")
        else:
            print("  ✓ Simbad already registered in OpenCode.")

    # 2. Claude Code
    claude_config = os.path.expanduser("~/.claude.json")
    if os.path.exists(claude_config):
        try:
            with open(claude_config, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
            
        mcp_servers = data.setdefault("mcpServers", {})
        if "simbad" not in mcp_servers:
            print("  Registering Simbad in Claude Code...")
            mcp_servers["simbad"] = {
                "command": "docker",
                "args": [
                    "run",
                    "-i",
                    "--rm",
                    "--network=host",
                    "-v",
                    "/home/alnar/Projects/MCP_SIMBAD/db:/app/db",
                    "simbad"
                ]
            }
            try:
                with open(claude_config, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                print("  ✓ Registered in Claude Code.")
            except Exception as e:
                print(f"  Error registering in Claude Code: {e}")
        else:
            print("  ✓ Simbad already registered in Claude Code.")

    # 3. Claude Desktop
    claude_desktop_config = os.path.expanduser("~/.config/Claude/claude_desktop_config.json")
    if os.path.isdir(os.path.expanduser("~/.config/Claude")):
        try:
            if os.path.exists(claude_desktop_config):
                with open(claude_desktop_config, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {}
        except Exception:
            data = {}
            
        mcp_servers = data.setdefault("mcpServers", {})
        if "simbad" not in mcp_servers:
            print("  Registering Simbad in Claude Desktop...")
            mcp_servers["simbad"] = {
                "command": "docker",
                "args": [
                    "run",
                    "-i",
                    "--rm",
                    "--network=host",
                    "-v",
                    "/home/alnar/Projects/MCP_SIMBAD/db:/app/db",
                    "simbad"
                ]
            }
            try:
                with open(claude_desktop_config, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                print("  ✓ Registered in Claude Desktop.")
            except Exception as e:
                print(f"  Error registering in Claude Desktop: {e}")
        else:
            print("  ✓ Simbad already registered in Claude Desktop.")

    # 4. Antigravity
    antigravity_config = os.path.expanduser("~/.gemini/config/mcp_config.json")
    if os.path.exists(antigravity_config) or os.path.isdir(os.path.expanduser("~/.gemini/config")):
        try:
            if os.path.exists(antigravity_config) and os.path.getsize(antigravity_config) > 0:
                with open(antigravity_config, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {}
        except Exception:
            data = {}
            
        mcp_servers = data.setdefault("mcpServers", {})
        if "simbad" not in mcp_servers:
            print("  Registering Simbad in Antigravity...")
            mcp_servers["simbad"] = {
                "command": "docker",
                "args": [
                    "run",
                    "-i",
                    "--rm",
                    "--network=host",
                    "-v",
                    "/home/alnar/Projects/MCP_SIMBAD/db:/app/db",
                    "simbad"
                ]
            }
            try:
                os.makedirs(os.path.dirname(antigravity_config), exist_ok=True)
                with open(antigravity_config, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                print("  ✓ Registered in Antigravity.")
            except Exception as e:
                print(f"  Error registering in Antigravity: {e}")
        else:
            print("  ✓ Simbad already registered in Antigravity.")

def main():
    print("🔄 Simbad Memory Sync is executing on host...")
    init_db()
    sync_claude_code()
    sync_antigravity()
    sync_opencode()
    try:
        os.chmod(DB_PATH, 0o666)
    except Exception:
        pass
    print("🔄 Sync complete! Simbad database is updated.")
    auto_register_mcp()

if __name__ == "__main__":
    main()
