import threading
from datetime import datetime
from typing import List, Dict, Any, Callable, Optional


class AgentLogger:
    def __init__(self, max_entries: int = 500):
        self._logs: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._seq = 0
        self._callback: Optional[Callable[[Dict[str, Any]], None]] = None

    def set_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._callback = callback

    def log(self, level: str, message: str, module: str = "SYSTEM") -> Dict[str, Any]:
        now_str = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._seq += 1
            entry = {
                "id": self._seq,
                "timestamp": now_str,
                "level": level.upper(),
                "module": module.upper(),
                "message": message,
            }
            self._logs.append(entry)
            if len(self._logs) > self._max_entries:
                self._logs = self._logs[-self._max_entries :]

        print(f"[{now_str}] [{entry['level']}] [{entry['module']}] {message}")

        if self._callback:
            try:
                self._callback(entry)
            except Exception:
                pass
        return entry

    def info(self, message: str, module: str = "SYSTEM") -> Dict[str, Any]:
        return self.log("INFO", message, module)

    def success(self, message: str, module: str = "SYSTEM") -> Dict[str, Any]:
        return self.log("SUCCESS", message, module)

    def warn(self, message: str, module: str = "SYSTEM") -> Dict[str, Any]:
        return self.log("WARN", message, module)

    def error(self, message: str, module: str = "SYSTEM") -> Dict[str, Any]:
        return self.log("ERROR", message, module)

    def get_logs_since(self, last_id: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            return [item for item in self._logs if item["id"] > last_id]

    def clear(self) -> None:
        with self._lock:
            self._logs.clear()
