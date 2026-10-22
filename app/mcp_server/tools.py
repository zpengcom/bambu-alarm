"""MCP tools shared by the stdio server (Xiaozhi tunnel) and the HTTP server."""

from app.common import store


def register(mcp):
    @mcp.tool()
    def get_printer_status(printer_name: str = "") -> list:
        """获取拓竹(Bambu Lab)3D打印机的当前状态：是否在线、打印状态、当前任务、进度、错误代码及含义。
        不传 printer_name 返回所有打印机；传名称（可部分匹配）只返回对应的打印机。
        当用户询问打印机/打印任务现在怎么样时使用。"""
        rows = store.printer_overview()
        if printer_name:
            rows = [r for r in rows if printer_name.lower() in r["name"].lower()]
        return rows

    @mcp.tool()
    def get_recent_alerts(limit: int = 5, printer_name: str = "") -> list:
        """获取最近的打印机报警记录（暂停、失败、完成、错误等），每条包含打印机名称、标题、内容和时间。
        当用户问"打印机最近有没有报警/出过什么问题"时使用。可用 printer_name 只看某一台。"""
        alerts = store.load_state()["alerts"]
        if printer_name:
            alerts = [a for a in alerts if printer_name.lower() in a.get("printer", "").lower()]
        return alerts[: max(1, min(limit, 50))]
