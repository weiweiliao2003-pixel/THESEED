"""One timeline across conversations, with local source and context inspection."""

import json

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QInputDialog, QLineEdit, QPlainTextEdit, QVBoxLayout

from seed.window import action, label, toolbar
from seed.workbench import ListPage


class ExperiencePage(ListPage):
    def __init__(self, window):
        super().__init__(window, "經歷時間線", "查找同一個種子的歷史訊息、封存會話與狀態，並回到來源。")
        self.query = QLineEdit(placeholderText="搜尋訊息、狀態或會話名稱", maxLength=300)
        self.query.setAccessibleName("搜尋經歷")
        self.query.returnPressed.connect(self.refresh)
        self.kind = QComboBox()
        for key, name in (("all", "全部來源"), ("message", "會話訊息"), ("state", "狀態紀錄")):
            self.kind.addItem(name, key)
        self.kind.currentIndexChanged.connect(self.refresh)
        controls = action("搜尋", self.refresh)
        row = QHBoxLayout()
        row.addWidget(self.query, 1)
        row.addWidget(self.kind)
        row.addWidget(controls)
        self.panel.insertLayout(0, row)
        self.progress = label("", "muted")
        self.panel.insertWidget(1, self.progress)
        toolbar(self.panel, action("開啟來源", self.open_source, primary=True),
                action("查詢模型請求紀錄", self.context_runs), action("重建來源索引", window.restart_experience))
        self.panel.addWidget(action("從選取經歷記錄成長", self.growth))
        self.pause = action("暫停索引", window.toggle_experience)
        self.panel.addWidget(self.pause)

    def refresh(self):
        identity = self.window.state.get_identity()["identity_id"]
        self.populate(self.window.journal.search(identity, self.query.text(), self.kind.currentData()),
                      lambda row: f"{row['occurred_at']} · {row['source_label']} · {row['conversation_title'] or '工作區'}"
                                  + ("（已封存）" if row["archived"] else "") + f"\n{row['excerpt']}")
        self.refresh_progress()

    def refresh_progress(self):
        if self.window.experience_active:
            self.progress.setText("正在分批補建來源索引，搜尋結果會逐步補齊。")
        elif self.window.experience_paused:
            self.progress.setText("補建已暫停，已建立的索引仍可查詢。")
        else:
            self.progress.setText("顯示最多 150 筆符合結果，包含已封存會話。")
        self.pause.setText("繼續索引" if self.window.experience_paused else "暫停索引")
        self.pause.setEnabled(self.window.experience_active or self.window.experience_paused)

    def show_details(self):
        row = self.selection()
        self.details.setPlainText(f"經歷 #{row['id']} · {row['kind']}\n來源 {row['source_type']}:{row['source_id']}\n"
                                  f"{row['excerpt']}\nSHA-256 {row['content_hash']}" if row else "尚無符合的經歷。")

    def growth(self):
        row = self.selection()
        if row:
            self.window.pages[15].edit(row["id"])

    def open_source(self):
        row = self.selection()
        if row is None:
            return
        source = self.window.journal.source(row["id"], self.window.state.get_identity()["identity_id"])
        if source is None:
            self.refresh()
            self.window.notice("來源已移除。")
        elif source["source_type"] == "message":
            self.window.pages[1].open_message_source(source["conversation_id"], source["message_id"], bool(source["archived"]))
        else:
            self.window.info(f"狀態來源 #{source['state_id']}", f"{source['occurred_at']} UTC · {source['source_label']}\n\n{source['content']}")

    def context_runs(self):
        identity = self.window.state.get_identity()["identity_id"]
        records = self.store.read("SELECT id,created_at,model,status FROM context_runs WHERE identity_id=? ORDER BY id DESC LIMIT 100", identity)
        choices = {f"#{row['id']} · {row['created_at']} · {row['model']} · {row['status']}": row["id"] for row in records}
        if not choices:
            self.window.notice("新版尚無模型請求紀錄。")
            return
        key, ok = QInputDialog.getItem(self, "模型請求紀錄", "最近 100 筆", list(choices), editable=False)
        if not ok:
            return
        self.show_run(choices[key])

    def show_run(self, run_id):
        identity = self.window.state.get_identity()["identity_id"]
        trace = self.window.journal.trace(run_id, identity)
        if not trace:
            self.window.notice("這則回覆沒有可用的來源紀錄。")
            return
        refs = json.loads(trace["source_refs"])
        groups = {"本輪訊息": [refs["current_request"]], "會話歷史": refs["recent_dialogue"],
                  "情境偏好": [refs["preference"]], "事件立場": [refs["position"]]}
        titles = []
        for kind, ids in groups.items():
            for event in ids:
                if event is None:
                    continue
                source = self.window.journal.source(event, identity)
                titles.append(f"{kind} · 經歷 #{event} · " + (f"{source['source_type']}:{source['source_id']}" if source else "來源已移除"))
        for memory in refs.get("retrieved_memories", []):
            ids = "、".join(f"#{ref}" for ref in memory["event_ids"]) or "無事件參照"
            titles.append(f"參考記憶 #{memory['id']} · 來源 {ids} · 摘錄 SHA-256 {memory['excerpt_sha256']}")
        recalled = []
        for item in refs.get("retrieved_experiences", []):
            source = self.window.journal.source(item["event_id"], identity)
            location = source["conversation_title"] if source else "來源已移除"
            if source:
                recalled.append(source)
            scope = "同會話" if item["conversation_id"] == trace["conversation_id"] else "跨會話"
            titles.append(f"{scope}經歷 #{item['event_id']} · {location} · {item['occurred_at']} UTC\n"
                          f"摘錄 SHA-256 {item['excerpt_sha256']}")
        guide = refs.get("growth_guidance")
        if guide:
            ids = "、".join(f"#{ref}" for ref in guide["evidence_ids"])
            titles.append(f"查證指引 #{guide['id']} · {guide['algorithm']} · 證據 {ids}")
        titles.extend(refs.get("notes", []))
        budget = refs.get("budget")
        if budget:
            titles.append(f"輸入估算 {budget['token_estimate']:,} / {budget['budget_limit']:,} Token · "
                          f"輸出保留 {budget['output_reserve']:,} · {budget['tokenizer_id']}\n"
                          f"略過來源 {len(budget['excluded_sources'])} 筆 · 摘要版本 {len(budget.get('topic_summary_versions', {}))} 筆")
        cue = refs.get("emotion_observation")
        if cue:
            observed = self.store.read("SELECT * FROM emotion_observations WHERE event_id=?", cue["event_id"])
            if observed:
                state = "已撤銷" if observed[0]["disposition"] == "dismissed" else "本輪推測"
                titles.append(f"{state} · {cue['label']} · 經歷 #{cue['event_id']} · 到期 {observed[0]['valid_until']} UTC")
        calls = self.store.read("SELECT * FROM model_calls WHERE run_id=? ORDER BY id", trace["id"])
        for row in calls:
            stage = "校對" if row["stage"] == "review" else "回應"
            titles.append(f"{stage} #{row['id']} · {row['model']} · {row['status']} · {row['duration_ms'] or 0} ms · {row['usage'] or '無用量回報'}")
            payload = refs.get("model_payloads", {}).get(str(row["id"]))
            if payload:
                histories = len(payload["dialogue_events"])
                cited = "、".join(f"#{ref}" for ref in payload["experience_ids"]) or "無"
                memory_ids = "、".join(f"#{ref}" for ref in payload["memory_ids"]) or "無"
                titles.append(f"端點 {payload['endpoint']} · 歷史 {histories} 則 · 經歷 {cited} · 記憶 {memory_ids}\n"
                              f"查證指引 {payload['growth_decision'] or '無'} · 草稿 {'有' if payload['includes_draft'] else '無'}\n"
                              f"提示 SHA-256 {payload['messages_sha256']}")
                for item in payload.get("attachments", ()):
                    stamp = item.get("timestamp_ms")
                    kind = "圖片" if stamp is None else f"抽樣影格 {stamp/1000:.3f} 秒"
                    titles.append(f"{kind} · {item['mime']} · SHA-256 {item['sha256']}")
        summary = (f"模型 {trace['model']} · 狀態 {trace['status']}\n"
                   f"人格版本 {trace['persona_version'] or '無'}\n歷史上限 {trace['history_limit']} 則，"
                   + ("" if budget else f"對話預算 {trace['character_limit']} 字元") + "\n\n" + "\n".join(titles))
        if not recalled:
            self.window.info("上下文來源", summary)
            return
        dialog = QDialog(self, windowTitle="上下文來源")
        dialog.resize(630, 460)
        layout = QVBoxLayout(dialog)
        details = QPlainTextEdit(readOnly=True)
        details.setPlainText(summary)
        layout.addWidget(details)
        choices = QComboBox()
        for source in recalled:
            choices.addItem(f"經歷 #{source['id']} · {source['conversation_title']}", source["id"])
        layout.addWidget(choices)
        def open_recalled():
            source = self.window.journal.source(choices.currentData(), identity)
            dialog.accept()
            if source:
                self.window.pages[1].open_message_source(source["conversation_id"], source["message_id"], bool(source["archived"]))
            else:
                self.window.notice("來源已移除。")
        toolbar(layout, action("開啟選取來源", open_recalled), action("關閉", dialog.reject))
        dialog.exec()
