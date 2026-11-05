# 地球速报

每天 3 分钟看完国际 + 马来西亚最重要的头条。

## 启动

```bash
cd "/Users/ch/Desktop/AI-APP/News everday"
python3 server.py
```

打开 http://localhost:8000 。不用装任何套件，只用 Python 自带的库。

## 怎么排“重要”

1. 编辑在 RSS 里排得越前面，分数越高
2. 越新分数越高（10 小时减半）
3. 多家媒体报道同一件事会合并成一条，报道的媒体越多越靠前（🔥 N 家媒体）
4. 马来西亚媒体转载的国际新闻会从大马版移除，避免重复

## 来源

国际：BBC、Al Jazeera、The Guardian、NPR、CNA、BBC 中文、DW 中文、RFI 中文
马来西亚：Free Malaysia Today、Malay Mail、Malaysiakini、Bernama、New Straits Times、The Vibes、The Sun、MalaysiaNow、当今大马、诗华日报

改来源：编辑 `server.py` 顶部的 `SOURCES`。

## 部署成静态网站

`python3 server.py --build` 生成 `data/news.json`，把 `index.html` 和 `data/` 一起上传到任何静态主机（GitHub Pages、Netlify 等），再用定时任务每 3 小时跑一次 `--build`。
