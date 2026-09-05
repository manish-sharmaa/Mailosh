"""View-model builders over `mailosh.jmap.client.JmapClient` (design spec
§12: "FastAPI routers stay thin and call `services/` view-model builders").

Task 6 adds `mailbox_tree` (the left nav) and `thread_list` (the list
page); `actions`/`undo` land in Task 9.
"""

from __future__ import annotations
