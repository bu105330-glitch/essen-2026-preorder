# essen-2026-preorder

SPIEL Essen 2026（2026/10/22–25）non-crowdfunding pre-order / pickup 收集清單。

- `index.html` — 靜態單頁網站，資料以 `const DATA = [...]` 內嵌於頁面中，依出版商分組，支援搜尋、篩選、排序。
- `scripts/build_site.py` — 從 BoardGameGeek geeklist [Essen 2026 Preorder Pickups](https://boardgamegeek.com/geeklist/380039/essen-2026-preorder-pickups) 重新抓取資料，改寫 `index.html` 內的 `DATA`。
- `.github/workflows/daily-update.yml` — 每日（Taipei 06:07）自動執行 `build_site.py` 並將變更 commit/push，觸發 Netlify 自動重新部署。

部署：Netlify（透過本 repo 的 Git 整合自動部署，push 到預設分支即會重新建置）。
