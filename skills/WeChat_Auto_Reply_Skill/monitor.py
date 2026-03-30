"""微信实时消息监听器（轮询解密 session.db）。

原理：定期解密 `session/session.db`，检测会话摘要变化来判断是否有新消息。
`session.db` 包含每个聊天的最新消息摘要、发送者、时间戳（但它不是完整消息表）。

核心思路：
- **session.db 不是实时 append 的“消息表”**，而是“会话摘要表”：每个会话只保留最近一条摘要。
- **WAL 很关键**：微信/SQLite 常把最新修改先写入 `session.db-wal`，若只读主库可能读到旧页。
- **last_timestamp 的语义**：`SessionTable.last_timestamp` 是消息的逻辑时间（秒级，通常来自发送侧/服务端），
  不是本地落盘时刻；因此 `time.time() - last_timestamp` 表示“时间戳相对本机时钟偏旧多少”，不等价于监听延迟。
  真正反映本地落盘新鲜度的是 `session.db` / `session.db-wal` 的 mtime（下面输出为“本地落盘≈”）。

本脚本的优化点（对齐 monitor_web / latency_test 的经验）：
- mtime 无变化则跳过解密（省 CPU）
- 解密主库后合并 WAL（避免未 checkpoint 读到旧数据）
- 同一秒内可能有多条消息：除 timestamp 前进外，也用 msg_type / summary 辅助判新
"""
import struct, os, sys, json, time, sqlite3, io, subprocess
from datetime import datetime
from Crypto.Cipher import AES
import zstandard as zstd
from key_utils import get_key_info, strip_key_metadata

_zstd_dctx = zstd.ZstdDecompressor()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 读取your_config.json文件
current_dir = os.path.dirname(os.path.abspath(__file__))
script_dir = os.path.join(current_dir, "config")
config = {}
try:
    with open(os.path.join(script_dir, "your_config.json"), "r", encoding="utf-8") as f:
        config = json.load(f)
    f.close()
except Exception as e:
    print(f"[ERROR] 读取your_config.json文件失败: {e}")
    sys.exit(1)

import functools
print = functools.partial(print, flush=True)

PAGE_SZ = 4096
SALT_SZ = 16
IV_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24

from config import load_config
from find_all_keys import main
main()
_cfg = load_config()
DB_DIR = _cfg["db_dir"]
KEYS_FILE = _cfg["keys_file"]
CONTACT_CACHE = os.path.join(_cfg["decrypted_dir"], "contact", "contact.db")

POLL_INTERVAL = config.get("poll_interval", 0.5)  # 秒；越小 CPU 越高，但“发现新消息”的上限延迟越低

_MONITOR_RECEIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_receive.py")
# 同一 display 在冷却内最多立即拉一次；冷却期内若仍有新消息则记 pending，冷却结束后再补拉一次
MONITOR_RECEIVE_COOLDOWN_SEC = config.get("monitor_receive_cooldown_sec", 15)
_monitor_receive_last_at: dict[str, float] = {}
_monitor_receive_pending: dict[str, bool] = {}


def _do_monitor_receive_spawn(chat_name: str) -> None:
    """真正启动 monitor_receive.py；成功则更新 last_at 并清除该 chat 的 pending。"""
    if not os.path.isfile(_MONITOR_RECEIVE):
        print(f"[WARN] 找不到 monitor_receive.py: {_MONITOR_RECEIVE}")
        return

    try:
        kw = {
            # "args": [sys.executable, _MONITOR_RECEIVE, chat_name],
            "args": f'python monitor_receive.py {chat_name}',
            # 当前文件夹=WeChat-Strong-MCP；父=.../mcp；父的父=.../workspace
            # 注意：这里是终端的当前工作目录，不是脚本所在目录
            "cwd": (os.path.dirname(_MONITOR_RECEIVE)),
            "shell":True,
        }
        # if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        #     kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(**kw)
        now = time.time()
        _monitor_receive_last_at[chat_name] = now
        _monitor_receive_pending.pop(chat_name, None)
        if len(_monitor_receive_last_at) > 2000:
            cutoff = now - MONITOR_RECEIVE_COOLDOWN_SEC * 20
            dead = [k for k, t in _monitor_receive_last_at.items() if t < cutoff]
            for k in dead:
                del _monitor_receive_last_at[k]
                _monitor_receive_pending.pop(k, None)
    except Exception as e:
        print(f"[WARN] 启动 monitor_receive.py 失败: {e}")


def _flush_pending_monitor_receive() -> None:
    """冷却结束后，若该 chat 在冷却期内有过新消息（pending），再补拉一次。"""
    now = time.time()
    for chat_name in list(_monitor_receive_pending.keys()):
        if not _monitor_receive_pending.get(chat_name):
            continue
        last = _monitor_receive_last_at.get(chat_name, 0)
        if now - last >= MONITOR_RECEIVE_COOLDOWN_SEC:
            _do_monitor_receive_spawn(chat_name)


def _spawn_monitor_receive(chat_name: str) -> None:
    """请求拉历史：不在冷却则立即拉；在冷却则只记 pending，由 _flush_pending_monitor_receive 在冷却后补拉。"""
    now = time.time()
    last = _monitor_receive_last_at.get(chat_name)
    if last is not None and (now - last) < MONITOR_RECEIVE_COOLDOWN_SEC:
        _monitor_receive_pending[chat_name] = True
        return
    _do_monitor_receive_spawn(chat_name)


def decrypt_page(enc_key, page_data, pgno):
    """解密单个 SQLCipher page。

    这里不做 HMAC 校验（对监听用途通常够用）；如果你想更严格的完整性校验，
    需要把 page 尾部的 HMAC 计算并比对（代价是性能更差）。
    """
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        page = bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ)
        return bytes(page)
    else:
        encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def decrypt_db_to_memory(db_path, enc_key):
    """解密主库到内存 bytes。

    注意：这里只处理主库文件，不包含 WAL；WAL 合并在 `decrypt_db_to_sqlite` 里做。
    """
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ
    if file_size % PAGE_SZ != 0:
        total_pages += 1

    chunks = []
    with open(db_path, 'rb') as fin:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page = page + b'\x00' * (PAGE_SZ - len(page))
                else:
                    break
            decrypted = decrypt_page(enc_key, page, pgno)
            chunks.append(decrypted)

    return b''.join(chunks)


def decrypt_wal_full(wal_path, out_path, enc_key):
    """解密 WAL 中当前有效 frame 并 patch 到已解密的主库副本。

    SQLite 的 WAL 文件常见特性：
    - WAL 可能是“预分配固定大小”的（不能用 size 判断是否有新数据）
    - WAL 中可能残留旧周期的 frame，需要用 WAL header 的 salt 过滤
    """
    if not os.path.exists(wal_path):
        return 0, 0.0
    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        return 0, 0.0

    t0 = time.perf_counter()
    frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ
    patched = 0

    with open(wal_path, 'rb') as wf, open(out_path, 'r+b') as df:
        wal_hdr = wf.read(WAL_HEADER_SZ)
        wal_salt1 = struct.unpack('>I', wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack('>I', wal_hdr[20:24])[0]

        while wf.tell() + frame_size <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack('>I', fh[0:4])[0]
            frame_salt1 = struct.unpack('>I', fh[8:12])[0]
            frame_salt2 = struct.unpack('>I', fh[12:16])[0]
            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break
            if pgno == 0 or pgno > 1000000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue
            dec = decrypt_page(enc_key, ep, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(dec)
            patched += 1

    return patched, (time.perf_counter() - t0) * 1000.0


def decrypt_db_to_sqlite(db_path, enc_key):
    """解密主库到临时文件，并合并 -wal 后再打开。

    为什么要先写临时文件：Python sqlite3 无法直接从 bytes 打开 SQLite。
    为什么要合并 WAL：否则 session.db 未 checkpoint 时，读到的还是旧页（看起来就像“timestamp 更新慢”）。
    """
    data = decrypt_db_to_memory(db_path, enc_key)
    tmp_path = db_path + ".tmp_monitor"
    with open(tmp_path, 'wb') as f:
        f.write(data)

    wal_path = db_path + "-wal"
    wal_pages = 0
    wal_ms = 0.0
    if os.path.exists(wal_path):
        wal_pages, wal_ms = decrypt_wal_full(wal_path, tmp_path, enc_key)

    conn = sqlite3.connect(tmp_path)
    conn.row_factory = sqlite3.Row
    return conn, tmp_path, wal_pages, wal_ms


def load_contact_names():
    """从已解密的 contact.db 加载联系人昵称映射。"""
    names = {}
    if not os.path.exists(CONTACT_CACHE):
        return names
    try:
        conn = sqlite3.connect(CONTACT_CACHE)
        rows = conn.execute(
            "SELECT username, nick_name, remark FROM contact"
        ).fetchall()
        for r in rows:
            username, nick, remark = r
            names[username] = remark if remark else nick if nick else username
        conn.close()
    except Exception as e:
        print(f"[WARN] 加载联系人失败: {e}")
    return names


def get_session_state(conn):
    """读取 SessionTable 的会话状态。"""
    state = {}
    try:
        rows = conn.execute("""
            SELECT username, unread_count, summary, last_timestamp,
                   last_msg_type, last_msg_sender, last_sender_display_name
            FROM SessionTable
            WHERE last_timestamp > 0
        """).fetchall()
        for r in rows:
            state[r[0]] = {
                'unread': r[1],
                'summary': r[2] or '',
                'timestamp': r[3],
                'msg_type': r[4],
                'sender': r[5] or '',
                'sender_name': r[6] or '',
            }
    except Exception as e:
        print(f"[ERROR] 读取session失败: {e}")
    return state


def format_msg_type(t):
    types = {
        1: '文本', 3: '图片', 34: '语音', 42: '名片',
        43: '视频', 47: '表情', 48: '位置', 49: '链接/文件/回复引用',
        50: '语音/视频通话', 10000: '系统消息', 10002: '撤回',
    }
    return types.get(t, f'type={t}')


def _session_fingerprint(entry):
    """生成“会话摘要指纹”用于同秒判新。

    微信的 SessionTable 往往只有秒级 timestamp；如果用户在 1 秒内发了多条，
    或出现“文字+图片组合消息”，timestamp 可能不变，但 msg_type/summary 会变。
    """
    s = entry.get('summary')
    if isinstance(s, bytes):
        return s
    return (s or '').encode('utf-8', errors='replace')


def _session_has_new_content(prev, curr):
    """判定会话是否出现新内容（含同秒多消息的退化情况）。"""
    if curr['timestamp'] > prev['timestamp']:
        return True
    if curr['timestamp'] < prev['timestamp']:
        return False
    if curr['msg_type'] != prev.get('msg_type'):
        return True
    return _session_fingerprint(curr) != _session_fingerprint(prev)

def _session_print_new_content(msg_type, summary):
    if isinstance(summary, bytes):
        try:
            summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
        except Exception:
            summary = '(压缩内容)'
    if summary:
        if ':\n' in summary:
            summary = summary.split(':\n', 1)[1]
        print(f"  [{msg_type}] {summary}")
    else:
        print(f"  [{msg_type}]")

def main():
    print("=" * 60)
    print("  微信实时消息监听器")
    print("=" * 60)

    # 加载密钥
    with open(KEYS_FILE, encoding="utf-8") as f:
        keys = strip_key_metadata(json.load(f))

    session_key_info = get_key_info(keys, os.path.join("session", "session.db"))
    if not session_key_info:
        print("[ERROR] 找不到session.db的密钥")
        sys.exit(1)

    enc_key = bytes.fromhex(session_key_info["enc_key"])
    session_db = os.path.join(DB_DIR, "session", "session.db")

    # 加载联系人
    print("加载联系人...")
    contact_names = load_contact_names()
    print(f"已加载 {len(contact_names)} 个联系人")

    # 初始状态
    print("读取初始状态...")
    conn, tmp_path, _, _ = decrypt_db_to_sqlite(session_db, enc_key)
    prev_state = get_session_state(conn)
    conn.close()
    os.remove(tmp_path)

    wal_path = session_db + "-wal"
    try:
        prev_wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
        prev_db_mtime = os.path.getmtime(session_db)
    except OSError:
        prev_wal_mtime = 0
        prev_db_mtime = 0

    print(f"跟踪 {len(prev_state)} 个会话")
    print(f"轮询间隔: {POLL_INTERVAL}秒")
    print(f"\n{'='*60}")
    print("开始监听... (Ctrl+C 停止)\n")


    try:
        while True:
            time.sleep(POLL_INTERVAL)
            _flush_pending_monitor_receive()

            try:
                try:
                    wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
                    db_mtime = os.path.getmtime(session_db)
                except OSError:
                    continue

                if wal_mtime == prev_wal_mtime and db_mtime == prev_db_mtime:
                    # 无变化则不解密；避免刷屏可改为每 N 次打印一次或静默
                    # print(
                    #     f"--- {datetime.now().strftime('%H:%M:%S')} 运行中 (session 无变化，跳过解密) ---"
                    # )
                    continue

                try:
                    session_db_age_s = (
                        min(time.time() - db_mtime, time.time() - wal_mtime)
                        if os.path.exists(wal_path)
                        else (time.time() - db_mtime)
                    )
                except Exception:
                    session_db_age_s = None

                conn, tmp_path, _wal_pages, _wal_ms = decrypt_db_to_sqlite(session_db, enc_key)

                curr_state = get_session_state(conn)

                conn.close()
                os.remove(tmp_path)

                prev_wal_mtime = wal_mtime
                prev_db_mtime = db_mtime
            except Exception as e:
                prev_wal_mtime = 0
                prev_db_mtime = 0
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 读取失败: {e}")
                continue

            # 比较差异
            for username, curr in curr_state.items():
                prev = prev_state.get(username)
                msg_type = format_msg_type(curr['msg_type'])
                summary = curr['summary']
                if prev is None:
                    display = contact_names.get(username, username)
                    ts = datetime.fromtimestamp(curr['timestamp']).strftime('%H:%M:%S')
                    # ts_delta_s = time.time() - curr['timestamp']
                    if session_db_age_s is not None:
                        print(
                            # f"[{ts}] 新会话 [{display}] (时间戳Δ: {ts_delta_s:.1f}s, 本地落盘≈: {session_db_age_s:.1f}s)"
                            f"[{ts}] 新会话[{display}]"
                        )
                    else:
                        # print(f"[{ts}] 新会话 [{display}] (时间戳Δ: {ts_delta_s:.1f}s)")
                        print(f"[{ts}]新会话 [{display}]")
                    # _spawn_monitor_receive(display)
                    _session_print_new_content(msg_type, summary)

                elif _session_has_new_content(prev, curr):
                    display = contact_names.get(username, username)
                    ts = datetime.fromtimestamp(curr['timestamp']).strftime('%H:%M:%S')
                    # ts_delta_s = time.time() - curr['timestamp']
                    sender = curr['sender_name'] or curr['sender'] or ''

                    # 群聊显示发送者
                    if '@chatroom' in username and sender:
                        sender_display = contact_names.get(curr['sender'], sender)
                        # if session_db_age_s is not None:
                        #     print(
                        #         f"[{ts}] [{display}] {sender_display}: (时间戳Δ: {ts_delta_s:.1f}s, 本地落盘≈: {session_db_age_s:.1f}s)"
                        #     )
                        # else:
                        #     print(f"[{ts}] [{display}] {sender_display}: (时间戳Δ: {ts_delta_s:.1f}s)")
                        print(f"[{ts}] [{display}] {sender_display}")
                    else:
                        # if session_db_age_s is not None:
                        #     print(f"[{ts}] [{display}] (时间戳Δ: {ts_delta_s:.1f}s, 本地落盘≈: {session_db_age_s:.1f}s)")
                        # else:
                        #     print(f"[{ts}] [{display}] (时间戳Δ: {ts_delta_s:.1f}s)")
                        print(f"[{ts}] [{display}]")
                    _session_print_new_content(msg_type, summary)
                    _spawn_monitor_receive(display)

            prev_state = curr_state

    except KeyboardInterrupt:
        print("\n监听结束 (Ctrl+C)")

    finally:
        # 清理临时解密文件（正常不会残留；异常退出时也尽量删掉）
        tmp = session_db + ".tmp_monitor"
        if os.path.exists(tmp):
            os.remove(tmp)


if __name__ == '__main__':
    main()
