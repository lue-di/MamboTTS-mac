"""下载工具：多源测速 + Range 断点续传（供 macOS 引擎安装器使用）。"""
import os
import threading
import time


_PROBE_BYTES = 2 * 1024 * 1024      # 每源测速采样 2MB
_PROBE_TIMEOUT = (6, 15)


def probe_source_speed(url):
    """单源测速：Range 拉取前 2MB 计时，返回字节/秒；失败返回 None。"""
    import requests  # 延迟导入：与 download_7z_tool 同理
    try:
        t0 = time.time()
        with requests.get(url, headers={"Range": f"bytes=0-{_PROBE_BYTES - 1}"},
                          stream=True, timeout=_PROBE_TIMEOUT,
                          proxies={"http": None, "https": None}) as r:
            if r.status_code not in (200, 206):
                return None
            got = 0
            for chunk in r.iter_content(chunk_size=65536):
                got += len(chunk)
                if got >= _PROBE_BYTES:
                    break
        dt = time.time() - t0
        return got / dt if dt > 0 else None
    except Exception:
        return None


def benchmark_sources(urls, log_cb):
    """并发测速，返回按速度降序的 URL 列表。

    全部失败时保留原始顺序（测速失败可能只是瞬时抖动，下载阶段再给机会），
    并输出每源成绩，方便用户判断网络状况。"""
    if not urls:
        return []
    results = {}

    def _probe(i, u):
        results[i] = probe_source_speed(u)

    threads = [threading.Thread(target=_probe, args=(i, u), daemon=True)
               for i, u in enumerate(urls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)

    scored = [(i, results.get(i)) for i in range(len(urls))]
    ok = sorted(((bps, i) for i, bps in scored if bps), reverse=True)
    dead = [urls[i] for i, bps in scored if not bps]
    for rank, (bps, i) in enumerate(ok, 1):
        log_cb(f"[测速] 第{rank}名 ≈{bps / (1024 * 1024):.1f}MB/s  {urls[i][:88]}")
    for u in dead:
        log_cb(f"[测速] 不可达，暂排除：{u[:88]}")
    if not ok:
        log_cb("[警告] 所有源测速失败（网络抖动或全被墙？），将按默认顺序直接尝试下载...")
        return urls
    return [urls[i] for _, i in ok] + dead


def download_file_resumable(url, dest_path, expected_bytes, progress_cb, log_cb,
                            cancel_cb, stage_base=0.0, stage_weight=0.0):
    """流式下载单源：.part 临时文件 + Range 断点续传 + 每块检查取消。

    换源续传安全策略（防止"换网络/换源后续传卡死"）：
    1. 每次对目标源发起续传前，都先发一个 0 字节 Range 探测头（bytes=0-0），
       确认该源支持 Range 且识别我们的偏移量，才带 Range 续传；
    2. 探测失败 / 返回 200 / 返回 416 / 中途异常——一律把 .part 降级为
       "从头重下"（清掉 .part 重新下载），而不是带着旧偏移反复撞墙。

    返回 True=文件完整；False=失败（.part 保留，换源或下次续传）。"""
    import requests
    part = dest_path + ".part"
    try:
        # .part 可能是目录/被占用/权限异常——一律按无断点从头处理，绝不崩 install()
        offset = os.path.getsize(part) if os.path.exists(part) else 0
    except OSError:
        offset = 0
    if offset >= expected_bytes:
        try:
            os.replace(part, dest_path)
        except OSError:
            return False
        return True

    # --- 服务端 Range 能力探测（仅当已有断点时） ---
    # 目的：换网络/换源后，旧偏移对当前源可能无效（源端内容变了/缓存清了），
    # 若直接带 Range 请求，源可能回 416/200，白等一次。先探测一次最稳。
    resumed = False
    if offset > 0:
        log_cb(f"[状态] 检测到断点 {offset / (1024 ** 3):.2f}GB，探测当前源是否支持续传...")
        try:
            probe = requests.get(url, headers={"Range": "bytes=0-0"},
                                 timeout=(10, 15),
                                 proxies={"http": None, "https": None})
            if probe.status_code == 206:
                content_range = probe.headers.get("Content-Range", "")
                # 形如 "bytes 0-0/9499785293"：尾数 = 服务端当前文件总长。
                total = content_range.rsplit("/", 1)[-1].strip()
                server_total = int(total) if total.isdigit() else None
                if server_total is not None and server_total < offset:
                    # 服务端文件比本地断点还短：旧断点已失效（源被换包/清缓存），从头下
                    log_cb("[警告] 服务端内容已变化（比本地断点短），旧断点失效，从头下载...")
                    offset = 0
                else:
                    resumed = True  # 服务端认可续传
            else:
                # 服务端忽略 Range / 不支持（200）或明确拒绝（416）→ 从头下
                log_cb("[提示] 该源不支持断点续传，将从头下载...")
                offset = 0
        except Exception as e:
            log_cb(f"[提示] 续传探测失败（{type(e).__name__}），将从头下载：{str(e)[:80]}")
            offset = 0

    headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
    mode = "ab" if offset > 0 else "wb"
    try:
        # 读超时 30s：网络半开时不再让进度条死等 60s 才报错，
        # 30s 内无任何数据即判定源卡死，换下一个源
        with requests.get(url, headers=headers, stream=True, timeout=(15, 30),
                          proxies={"http": None, "https": None}) as r:
            if offset > 0 and r.status_code == 206:
                pass  # 服务端确认断点续传
            elif offset > 0 and r.status_code == 200:
                # 探测通过但实际下载时忽略了 Range（代理层）→ 从头重写
                log_cb("[提示] 下载时源忽略了 Range，从头重新下载...")
                offset = 0
                mode = "wb"
            elif r.status_code not in (200, 206):
                log_cb(f"[警告] 源返回 HTTP {r.status_code}，换源：{url[:64]}")
                return False
            with open(part, mode) as f:
                if offset > 0:
                    f.seek(offset)
                    off_str = (f"{offset / (1024 ** 3):.2f}GB" if offset >= 1024 ** 3
                               else f"{offset / (1024 ** 2):.0f}MB")
                    log_cb(f"[状态] 断点续传：从 {off_str} 处继续")
                else:
                    log_cb("[状态] 开始下载...")
                last_log = time.time()
                last_log_bytes = offset
                while True:
                    chunk = r.raw.read(1024 * 1024, decode_content=True)
                    if not chunk:
                        break
                    if cancel_cb():
                        return False
                    f.write(chunk)
                    offset += len(chunk)
                    if progress_cb and stage_weight > 0:
                        progress_cb(stage_base + min(offset / expected_bytes, 0.95) * stage_weight)
                    now = time.time()
                    if now - last_log >= 5:
                        mbps = (offset - last_log_bytes) / (now - last_log) / (1024 * 1024)
                        remain = max(expected_bytes - offset, 0) / (1024 ** 3)
                        log_cb(f"[状态] 已下载 {offset / (1024 ** 3):.2f}GB / "
                               f"{expected_bytes / (1024 ** 3):.2f}GB | "
                               f"速度 {mbps:.1f}MB/s | 剩余 {remain:.2f}GB")
                        last_log = now
                        last_log_bytes = offset
        if offset >= expected_bytes:
            os.replace(part, dest_path)
            return True
        log_cb(f"[警告] 该源中途截断（{offset}/{expected_bytes} 字节），尝试下一个源续传...")
        return False
    except Exception as e:
        log_cb(f"[警告] 源下载异常（{type(e).__name__}），尝试下一个源：{str(e)[:100]}")
        return False


