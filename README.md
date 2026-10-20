# leak-guard

上傳或 `git push` 前的最後一道檢查：掃描發版檔案（會遞迴打開 zip 與 gzip）或這次 push 會送出的 git 物件，只要出現設定的敏感字詞（帳號名、內部主機名、本機路徑等）就擋下。

只用 Python 標準函式庫，單一檔案，可直接複製進任何專案使用。

## 設計原則

- **字詞不進版控**：要找的字詞只存在 repo 以外的本機檔案。原始碼、CI 設定、log 都不含它。
- **Fail-closed**：讀不到字詞檔或格式不對，一律不放行（exit 2）。檢查工具本身壞掉時，不能變成「全部通過」。
- **輸出不洩漏**：訊息只列檔名、壓縮檔內的成員路徑與命中次數，永遠不印出字詞本身。
- **只看會送出去的東西**：`git` 模式只掃遠端還沒有的 commit、tree、blob 和 annotated tag，不重掃整個歷史。

## 判定規則

一次「真命中」必須同時符合：

1. 出現在**字詞開頭**（前一個字元不是英數或底線）：`/home/octocat/` 會命中，`myoctocat` 不會。
2. 大小寫是**全小寫、首字大寫或全大寫**其中之一：`OctoCat` 這類怪大小寫視為巧合，不擋。

除了一般文字，也會偵測 **UTF-16LE**（Windows 程式常見）和 **base64** 編碼後的字詞。

壓縮檔最多遞迴兩層。壞掉的壓縮檔會在結果中標示 `<container-error>`，不會被靜默略過。

## 安裝

```sh
pip install git+https://github.com/Lother/leak-guard
```

或直接把 `leak_guard.py` 複製進專案，用 `python leak_guard.py` 執行。

## 設定字詞檔

預設路徑是 `~/.config/leak-guard/needles.txt`，也可以用環境變數 `LEAK_GUARD_NEEDLE_FILE` 或參數 `--needle-file` 指定。

```text
# 一行一個，大小寫不拘；# 之後是註解
octocat
build-server-01   # 內部主機名
```

每個字詞至少 4 個字元，只能包含 `a-z`、`0-9`、`_`、`.`、`-`。

## 使用方式

```sh
# 掃描發版檔案
leak-guard files dist/*.zip dist/*.tar.gz

# 掃描 push 某個 ref 會送出的物件
leak-guard git v1.2.0 --repo . --remote origin
```

| Exit code | 意義 |
|---|---|
| `0` | 乾淨，可以放行 |
| `1` | 擋下，有真命中（列出 `BLOCK` 行） |
| `2` | 設定或執行錯誤，不放行 |

### 當成 git pre-push hook

```sh
cp hooks/pre-push .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

每次 `git push` 都會先掃描要送出的物件，有命中就中止 push。

### 在 CI 中使用

```yaml
- name: leak-guard
  env:
    LEAK_GUARD_NEEDLE_FILE: ${{ runner.temp }}/needles.txt
  run: |
    printf '%s\n' "$NEEDLES" > "$LEAK_GUARD_NEEDLE_FILE"
    leak-guard files dist/*
  # NEEDLES 來自 repository secret，不寫進 workflow
```

## 測試

```sh
python -m unittest discover -s tests -v
```

測試涵蓋大小寫與字詞邊界判定、UTF-16LE 與 base64、巢狀 zip 和 gzip、字詞檔 fail-closed，以及 `git` 模式只掃描新物件與 annotated tag。

## 背景

這個工具原本是一套多 repo 自動發版管線的一部分，用來在發版前擋下帶有帳號痕跡的建置產物，後來抽出成獨立工具。設計脈絡見[多 repo 發版管線的四個設計決策](https://lother.github.io/notes/release-pipeline.html)。

以 AI coding agent 協作開發：需求、規則設計與審查由作者負責。

## 授權

MIT
