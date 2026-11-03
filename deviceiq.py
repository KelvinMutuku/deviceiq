#!/usr/bin/env python3
"""DEVICEIQ - cross-platform Android refurbishment diagnostic workstation."""
import os, sys, json, csv, sqlite3, subprocess, threading, queue, re, time, platform, webbrowser
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME="DEVICEIQ"
DB_PATH=Path.home()/".deviceiq"/"deviceiq.sqlite3"
ADB_CANDIDATES=[]
if os.environ.get("ANDROID_HOME"): ADB_CANDIDATES += [Path(os.environ["ANDROID_HOME"])/"platform-tools"/("adb.exe" if os.name=="nt" else "adb")]
if os.environ.get("ANDROID_SDK_ROOT"): ADB_CANDIDATES += [Path(os.environ["ANDROID_SDK_ROOT"])/"platform-tools"/("adb.exe" if os.name=="nt" else "adb")]
ADB_CANDIDATES += [Path("platform-tools")/("adb.exe" if os.name=="nt" else "adb"), Path("adb.exe" if os.name=="nt" else "adb")]

def now(): return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
def safe_filename(s): return re.sub(r"[^A-Za-z0-9_.-]+","_",s or "device")

class Store:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.db=sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS devices(id INTEGER PRIMARY KEY, serial TEXT, manufacturer TEXT, model TEXT, product TEXT, android TEXT, sdk TEXT, fingerprint TEXT, first_seen TEXT, last_seen TEXT);
        CREATE TABLE IF NOT EXISTS inspections(id INTEGER PRIMARY KEY, device_id INTEGER, started TEXT, finished TEXT, status TEXT, score REAL, grade TEXT, operator TEXT, notes TEXT, payload TEXT, FOREIGN KEY(device_id) REFERENCES devices(id));
        CREATE TABLE IF NOT EXISTS test_results(id INTEGER PRIMARY KEY, inspection_id INTEGER, category TEXT, test_name TEXT, outcome TEXT, details TEXT, tested_at TEXT, FOREIGN KEY(inspection_id) REFERENCES inspections(id));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT, action TEXT, detail TEXT);
        """)
        self.db.commit()
    def log(self,action,detail=""):
        self.db.execute("INSERT INTO audit(at,action,detail) VALUES(?,?,?)",(now(),action,detail)); self.db.commit()
    def upsert_device(self,d):
        cur=self.db.execute("SELECT id FROM devices WHERE serial=?",(d.get("serial"),)); row=cur.fetchone()
        vals=(d.get("serial"),d.get("manufacturer"),d.get("model"),d.get("product"),d.get("android"),d.get("sdk"),d.get("fingerprint"),now())
        if row:
            self.db.execute("""UPDATE devices SET serial=?,manufacturer=?,model=?,product=?,android=?,sdk=?,fingerprint=?,last_seen=? WHERE id=?""",vals+(row["id"],)); did=row["id"]
        else:
            c=self.db.execute("""INSERT INTO devices(serial,manufacturer,model,product,android,sdk,fingerprint,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?)""",vals[:7]+(now(),now())); did=c.lastrowid
        self.db.commit(); return did
    def start_inspection(self,did,operator):
        c=self.db.execute("INSERT INTO inspections(device_id,started,status,operator) VALUES(?,?,?,?)",(did,now(),"IN_PROGRESS",operator)); self.db.commit(); return c.lastrowid
    def add_result(self,iid,category,name,outcome,details):
        self.db.execute("INSERT INTO test_results(inspection_id,category,test_name,outcome,details,tested_at) VALUES(?,?,?,?,?,?)",(iid,category,name,outcome,details,now())); self.db.commit()
    def finish(self,iid,status,score,grade,notes,payload):
        self.db.execute("UPDATE inspections SET finished=?,status=?,score=?,grade=?,notes=?,payload=? WHERE id=?",(now(),status,score,grade,notes,json.dumps(payload,ensure_ascii=False),iid)); self.db.commit()
    def history(self):
        return self.db.execute("""SELECT i.id,i.started,i.finished,i.status,i.score,i.grade,i.operator,d.manufacturer,d.model,d.serial FROM inspections i JOIN devices d ON d.id=i.device_id ORDER BY i.id DESC LIMIT 250""").fetchall()
    def close(self): self.db.close()

class ADB:
    def __init__(self): self.path=None
    def locate(self):
        import shutil
        p=shutil.which("adb")
        if p: self.path=p; return p
        for x in ADB_CANDIDATES:
            if x.is_file(): self.path=str(x.resolve()); return self.path
        return None
    def run(self,args,timeout=20):
        if not self.path and not self.locate(): raise RuntimeError("ADB not found. Install Android SDK Platform-Tools and add adb to PATH.")
        cmd=[self.path]+list(args)
        cp=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout,encoding="utf-8",errors="replace")
        if cp.returncode!=0: raise RuntimeError((cp.stderr or cp.stdout or f"ADB exit {cp.returncode}").strip())
        return cp.stdout.strip()
    def devices(self):
        out=self.run(["devices","-l"])
        result=[]
        for line in out.splitlines()[1:]:
            p=line.split()
            if len(p)>=2 and p[1] in ("device","unauthorized","offline"):
                result.append({"serial":p[0],"state":p[1],"description":" ".join(p[2:])})
        return result
    def shell(self,serial,command,timeout=25): return self.run(["-s",serial,"shell"]+command.split(),timeout=timeout)
    def prop(self,serial,key): return self.shell(serial,f"getprop {key}")
    def info(self,serial):
        keys={"manufacturer":"ro.product.manufacturer","model":"ro.product.model","product":"ro.product.name","android":"ro.build.version.release","sdk":"ro.build.version.sdk","fingerprint":"ro.build.fingerprint"}
        d={"serial":serial}
        for k,v in keys.items():
            try: d[k]=self.prop(serial,v)
            except Exception: d[k]=""
        return d
    def battery(self,serial): return self.shell(serial,"dumpsys battery")
    def diagnostics(self,serial):
        data={"device":self.info(serial)}
        commands={
          "battery":"dumpsys battery","memory":"dumpsys meminfo","storage":"df -h /data",
          "cpu":"cat /proc/cpuinfo","uptime":"cat /proc/uptime","display":"wm size && wm density",
          "network":"ip addr","wifi":"dumpsys wifi","bluetooth":"dumpsys bluetooth_manager",
          "sensors":"dumpsys sensorservice","thermal":"dumpsys thermalservice",
          "packages":"pm list packages -3","security":"getprop ro.boot.verifiedbootstate && getprop ro.boot.flash.locked",
        }
        for k,c in commands.items():
            try: data[k]=self.shell(serial,c,timeout=35)
            except Exception as e: data[k]=f"UNAVAILABLE: {e}"
        return data
    def launch(self,serial,kind):
        intents={
          "Camera":"am start -a android.media.action.IMAGE_CAPTURE",
          "Dialer":"am start -a android.intent.action.DIAL",
          "Wi-Fi settings":"am start -a android.settings.WIFI_SETTINGS",
          "Bluetooth settings":"am start -a android.settings.BLUETOOTH_SETTINGS",
          "Display settings":"am start -a android.settings.DISPLAY_SETTINGS",
          "Sound settings":"am start -a android.settings.SOUND_SETTINGS",
          "Location settings":"am start -a android.settings.LOCATION_SOURCE_SETTINGS",
          "Fingerprint settings":"am start -a android.settings.SECURITY_SETTINGS",
        }
        return self.shell(serial,intents[kind],timeout=15)
    def pull_logcat(self,serial): return self.run(["-s",serial,"logcat","-d","-t","1500"],timeout=40)

class App:
    def __init__(self,root):
        self.root=root; self.root.title("DEVICEIQ | Android Diagnostic Workstation"); self.root.geometry("1200x790"); self.root.minsize(980,650)
        self.adb=ADB(); self.store=Store(); self.q=queue.Queue(); self.device=None; self.device_info={}; self.device_id=None; self.inspection_id=None; self.results={}; self.last_diag={}
        self.style=ttk.Style()
        try:self.style.theme_use("clam")
        except:pass
        self.style.configure("Treeview",rowheight=27); self.style.configure("Title.TLabel",font=("Arial",18,"bold"))
        self.build(); self.refresh_devices(); self.poll()
        self.root.protocol("WM_DELETE_WINDOW",self.close)
    def build(self):
        top=ttk.Frame(self.root,padding=12); top.pack(fill="x")
        ttk.Label(top,text="DEVICEIQ",style="Title.TLabel").pack(side="left")
        ttk.Label(top,text="  ANDROID QUALITY & DIAGNOSTICS WORKSTATION",foreground="#64748b").pack(side="left",pady=7)
        self.adb_label=ttk.Label(top,text="ADB: checking…"); self.adb_label.pack(side="right")
        bar=ttk.Frame(self.root,padding=(12,0,12,10)); bar.pack(fill="x")
        ttk.Label(bar,text="Connected device:").pack(side="left")
        self.device_var=tk.StringVar()
        self.device_combo=ttk.Combobox(bar,textvariable=self.device_var,state="readonly",width=55); self.device_combo.pack(side="left",padx=8)
        ttk.Button(bar,text="Refresh",command=self.refresh_devices).pack(side="left")
        ttk.Button(bar,text="Connect over TCP/IP…",command=self.tcp_dialog).pack(side="left",padx=6)
        self.status=ttk.Label(bar,text="No device selected",foreground="#b45309"); self.status.pack(side="right")
        self.notebook=ttk.Notebook(self.root); self.notebook.pack(fill="both",expand=True,padx=12,pady=6)
        self.tab_over=ttk.Frame(self.notebook,padding=12); self.tab_tests=ttk.Frame(self.notebook,padding=12); self.tab_report=ttk.Frame(self.notebook,padding=12); self.tab_history=ttk.Frame(self.notebook,padding=12)
        for t,n in [(self.tab_over,"Device & Diagnostics"),(self.tab_tests,"Interactive Tests"),(self.tab_report,"Inspection & Reports"),(self.tab_history,"History & Audit")]: self.notebook.add(t,text=n)
        self.build_overview(); self.build_tests(); self.build_report(); self.build_history()
    def build_overview(self):
        f=self.tab_over
        self.info_text=tk.Text(f,height=9,wrap="word",font=("Consolas",10)); self.info_text.pack(fill="x")
        row=ttk.Frame(f); row.pack(fill="x",pady=8)
        ttk.Button(row,text="Read device information",command=self.read_info).pack(side="left")
        ttk.Button(row,text="Run ADB diagnostics",command=self.run_diagnostics).pack(side="left",padx=6)
        ttk.Button(row,text="Save diagnostic snapshot",command=self.save_snapshot).pack(side="left")
        ttk.Button(row,text="Export raw diagnostics",command=self.export_raw).pack(side="left",padx=6)
        self.diag_text=tk.Text(f,wrap="word",font=("Consolas",9)); self.diag_text.pack(fill="both",expand=True)
    def build_tests(self):
        f=self.tab_tests
        ttk.Label(f,text="ADB-triggered checks open the relevant screen/app on the phone. The technician must observe the result and record the outcome.").pack(anchor="w",pady=(0,8))
        self.test_tree=ttk.Treeview(f,columns=("category","outcome","details"),show="headings",height=15)
        for c,w in [("category",170),("outcome",120),("details",650)]:
            self.test_tree.heading(c,text=c.title()); self.test_tree.column(c,width=w)
        self.test_tree.pack(fill="both",expand=True)
        self.test_items=[
          ("Display","Display / touch panel","Open Display settings; inspect screen, touch zones, colours and burn-in."),
          ("Camera","Rear/front cameras","Launch camera; test every lens, focus, flash, photo and video."),
          ("Audio","Earpiece / speaker / microphone","Use a test call or voice recorder; verify input/output clarity."),
          ("Connectivity","Wi-Fi","Open Wi-Fi settings; connect to a known network and verify data."),
          ("Connectivity","Bluetooth","Open Bluetooth settings; pair with an authorized test accessory."),
          ("Connectivity","Cellular / SIM / calls","Verify SIM registration, outgoing/incoming calls and mobile data."),
          ("Sensors","Proximity / light / motion","Use supported manufacturer diagnostics or a sensor test app; verify response."),
          ("Biometrics","Fingerprint / face unlock","Open security settings; enroll/test only with owner authorization."),
          ("Charging","USB / charging","Connect approved charger; verify stable charging and data connection."),
          ("Location","GPS / location","Open location settings and verify location fix using an approved test app."),
          ("Physical","Buttons / vibration","Manually test power, volume, haptics, ports and SIM tray."),
        ]
        for cat,name,desc in self.test_items: self.test_tree.insert("","end",values=(name,"NOT TESTED",desc))
        controls=ttk.Frame(f); controls.pack(fill="x",pady=8)
        ttk.Button(controls,text="Trigger selected test on phone",command=self.trigger_test).pack(side="left")
        ttk.Button(controls,text="Mark selected PASS",command=lambda:self.mark_test("PASS")).pack(side="left",padx=5)
        ttk.Button(controls,text="Mark selected FAIL",command=lambda:self.mark_test("FAIL")).pack(side="left")
        ttk.Button(controls,text="Mark selected N/A",command=lambda:self.mark_test("N/A")).pack(side="left",padx=5)
        ttk.Button(controls,text="Add test notes",command=self.test_notes).pack(side="left")
    def build_report(self):
        f=self.tab_report
        row=ttk.Frame(f); row.pack(fill="x",pady=4)
        ttk.Label(row,text="Technician / operator:").pack(side="left")
        self.operator=tk.StringVar(value=os.environ.get("USERNAME") or os.environ.get("USER") or "QC Technician")
        ttk.Entry(row,textvariable=self.operator,width=24).pack(side="left",padx=6)
        ttk.Label(row,text="Inspection notes:").pack(side="left",padx=(15,4))
        self.notes=tk.StringVar()
        ttk.Entry(row,textvariable=self.notes,width=48).pack(side="left")
        buttons=ttk.Frame(f); buttons.pack(fill="x",pady=10)
        ttk.Button(buttons,text="Start inspection",command=self.start_inspection).pack(side="left")
        ttk.Button(buttons,text="Calculate score & finalize",command=self.finalize).pack(side="left",padx=6)
        ttk.Button(buttons,text="Export PDF / HTML report",command=self.export_report).pack(side="left")
        ttk.Button(buttons,text="Export CSV results",command=self.export_csv).pack(side="left",padx=6)
        self.score_label=ttk.Label(f,text="No inspection finalized",font=("Arial",16,"bold")); self.score_label.pack(anchor="w",pady=15)
        self.report_text=tk.Text(f,wrap="word"); self.report_text.pack(fill="both",expand=True)
    def build_history(self):
        f=self.tab_history
        self.hist=ttk.Treeview(f,columns=("id","date","device","serial","status","score","grade","operator"),show="headings")
        for c,w in [("id",55),("date",160),("device",210),("serial",180),("status",130),("score",75),("grade",100),("operator",130)]:
            self.hist.heading(c,text=c.title()); self.hist.column(c,width=w)
        self.hist.pack(fill="both",expand=True)
        row=ttk.Frame(f); row.pack(fill="x",pady=8)
        ttk.Button(row,text="Refresh history",command=self.refresh_history).pack(side="left")
        ttk.Button(row,text="Export audit log",command=self.export_audit).pack(side="left",padx=6)
    def selected_serial(self):
        val=self.device_var.get()
        return val.split(" | ")[0] if val else None
    def async_task(self,fn,success=None):
        def work():
            try:self.q.put(("ok",fn(),success))
            except Exception as e:self.q.put(("err",str(e),None))
        threading.Thread(target=work,daemon=True).start()
    def poll(self):
        try:
            while True:
                typ,data,cb=self.q.get_nowait()
                if typ=="err": messagebox.showerror("DEVICEIQ",data)
                elif cb: cb(data)
        except queue.Empty: pass
        self.root.after(150,self.poll)
    def refresh_devices(self):
        self.status.config(text="Scanning ADB…")
        def done(devs):
            vals=[f'{d["serial"]} | {d["state"]} | {d["description"]}' for d in devs]
            self.device_combo["values"]=vals
            if vals and self.device_var.get() not in vals:self.device_var.set(vals[0])
            self.adb_label.config(text=f"ADB: {self.adb.path or 'not found'}")
            self.status.config(text=f"{len(devs)} device(s) detected")
            if devs and devs[0]["state"]=="unauthorized": self.status.config(text="Authorize USB debugging on the phone")
        self.async_task(lambda:self.adb.devices(),done)
    def require_device(self):
        s=self.selected_serial()
        if not s: raise RuntimeError("Connect an Android device and select it first.")
        return s
    def read_info(self):
        s=self.require_device()
        def done(d):
            if not d.get("manufacturer"): messagebox.showwarning("Device", "No device properties returned. Check USB debugging authorization.")
            self.device_info=d; self.device=s; self.device_id=self.store.upsert_device(d)
            self.info_text.delete("1.0","end"); self.info_text.insert("end","\n".join(f"{k.upper():14}: {v}" for k,v in d.items()))
            self.store.log("DEVICE_READ",s); self.status.config(text=f"Selected: {d.get('manufacturer')} {d.get('model')}")
        self.async_task(lambda:self.adb.info(s),done)
    def run_diagnostics(self):
        s=self.require_device(); self.status.config(text="Running diagnostics…")
        def done(d):
            self.last_diag=d
            self.diag_text.delete("1.0","end")
            for k,v in d.items():
                self.diag_text.insert("end",f"\n{'='*22} {k.upper()} {'='*22}\n{str(v)[:12000]}\n")
            self.device_info=d["device"]; self.device_id=self.store.upsert_device(d["device"])
            self.store.log("ADB_DIAGNOSTICS",s); self.status.config(text="Diagnostics completed; review results")
        self.async_task(lambda:self.adb.diagnostics(s),done)
    def save_snapshot(self):
        if not self.last_diag: messagebox.showinfo("Snapshot","Run ADB diagnostics first."); return
        p=filedialog.asksaveasfilename(defaultextension=".json",filetypes=[("JSON","*.json")],initialfile="deviceiq_diagnostics.json")
        if p:
            Path(p).write_text(json.dumps(self.last_diag,indent=2),encoding="utf-8"); self.store.log("SNAPSHOT_EXPORTED",p)
    def export_raw(self):
        if not self.last_diag: messagebox.showinfo("Diagnostics","Run diagnostics first."); return
        p=filedialog.asksaveasfilename(defaultextension=".txt",filetypes=[("Text","*.txt")],initialfile="deviceiq_raw_diagnostics.txt")
        if p:
            with open(p,"w",encoding="utf-8") as f:
                for k,v in self.last_diag.items(): f.write(f"\n===== {k.upper()} =====\n{v}\n")
    def trigger_test(self):
        s=self.require_device(); sel=self.test_tree.selection()
        if not sel: messagebox.showinfo("Test","Select a test first."); return
        vals=self.test_tree.item(sel[0],"values"); name=vals[0]
        mapping={"Wi-Fi":"Wi-Fi settings","Bluetooth":"Bluetooth settings","Rear/front cameras":"Camera","Display / touch panel":"Display settings","Earpiece / speaker / microphone":"Sound settings","Proximity / light / motion":"Display settings","Fingerprint / face unlock":"Fingerprint settings","USB / charging":"Display settings","GPS / location":"Location settings","Buttons / vibration":"Sound settings","Cellular / SIM / calls":"Dialer"}
        kind=mapping.get(name)
        if not kind:return
        self.async_task(lambda:self.adb.launch(s,kind),lambda r:messagebox.showinfo("Test triggered",f"Opened {kind} on the phone.\n\nNow perform the physical test and record its result.\n\n{r}"))
    def mark_test(self,outcome):
        sel=self.test_tree.selection()
        if not sel: messagebox.showinfo("Test","Select a test first."); return
        vals=list(self.test_tree.item(sel[0],"values")); vals[1]=outcome
        self.test_tree.item(sel[0],values=vals)
        if outcome=="PASS": self.results[vals[0]]={"category":vals[0],"outcome":outcome,"details":vals[2]}
        else:self.results[vals[0]]={"category":vals[0],"outcome":outcome,"details":vals[2]}
        if self.inspection_id:self.store.add_result(self.inspection_id,vals[0],vals[0],outcome,vals[2])
    def test_notes(self):
        sel=self.test_tree.selection()
        if not sel:return
        from tkinter.simpledialog import askstring
        note=askstring("Test notes","Enter observation / evidence reference:")
        if note is not None:
            vals=list(self.test_tree.item(sel[0],"values")); vals[2]=note; self.test_tree.item(sel[0],values=vals)
            if vals[0] in self.results:self.results[vals[0]]["details"]=note
    def start_inspection(self):
        if not self.device_id:self.read_info(); return
        if self.inspection_id and messagebox.askyesno("Inspection","An inspection is already open. Start another?") is False:return
        self.inspection_id=self.store.start_inspection(self.device_id,self.operator.get().strip() or "QC Technician"); self.results={}
        for item in self.test_tree.get_children():
            vals=list(self.test_tree.item(item,"values")); vals[1]="NOT TESTED"; self.test_tree.item(item,values=vals)
        self.store.log("INSPECTION_STARTED",f"id={self.inspection_id}")
        messagebox.showinfo("Inspection",f"Inspection #{self.inspection_id} started.")
    def finalize(self):
        if not self.inspection_id: messagebox.showwarning("Inspection","Start an inspection first."); return
        rows=[self.test_tree.item(x,"values") for x in self.test_tree.get_children()]
        total=len(rows); passed=sum(1 for r in rows if r[1]=="PASS"); failed=sum(1 for r in rows if r[1]=="FAIL"); na=sum(1 for r in rows if r[1]=="N/A")
        applicable=total-na; score=round((passed/applicable)*100,1) if applicable else 0
        critical_fail=any(r[1]=="FAIL" and r[0] in ("USB / charging","Display / touch panel","Cellular / SIM / calls") for r in rows)
        untested=any(r[1]=="NOT TESTED" for r in rows)
        status="REJECTED" if failed or critical_fail else ("INCOMPLETE" if untested else "APPROVED")
        grade=("Premium" if score>=95 else "Excellent" if score>=90 else "Very Good" if score>=80 else "REWORK")
        if status!="APPROVED": grade="REWORK / HOLD"
        payload={"tests":[{"name":r[0],"outcome":r[1],"details":r[2]} for r in rows],"diagnostics":self.last_diag}
        self.store.finish(self.inspection_id,status,score,grade,self.notes.get(),payload)
        self.score_label.config(text=f"{status}  |  {score}%  |  {grade}")
        self.report_text.delete("1.0","end")
        self.report_text.insert("end",f"DEVICEIQ INSPECTION #{self.inspection_id}\nDevice: {self.device_info.get('manufacturer','')} {self.device_info.get('model','')}\nSerial: {self.device_info.get('serial','')}\nOperator: {self.operator.get()}\nCompleted: {now()}\nScore: {score}%\nGrade: {grade}\nStatus: {status}\n\n")
        for r in rows:self.report_text.insert("end",f"{r[0]:38} {r[1]:14} {r[2]}\n")
        self.store.log("INSPECTION_FINALIZED",f"id={self.inspection_id};status={status};score={score}")
        self.refresh_history()
        messagebox.showinfo("Inspection finalized",f"{status}\nScore: {score}%\nGrade: {grade}")
    def export_report(self):
        if not self.inspection_id: messagebox.showinfo("Report","Finalize an inspection first."); return
        p=filedialog.asksaveasfilename(defaultextension=".html",filetypes=[("HTML report","*.html")],initialfile=f"DEVICEIQ_inspection_{self.inspection_id}.html")
        if not p:return
        rows=[self.test_tree.item(x,"values") for x in self.test_tree.get_children()]
        html=["<!doctype html><html><head><meta charset='utf-8'><title>DEVICEIQ Inspection</title><style>body{font:14px Arial;margin:40px;color:#172033}h1{color:#153b66}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:9px;text-align:left}.pass{color:green}.fail{color:#b91c1c}</style></head><body>"]
        html.append(f"<h1>DEVICEIQ Inspection Certificate</h1><p>Inspection #{self.inspection_id}</p><h2>{self.device_info.get('manufacturer','')} {self.device_info.get('model','')}</h2>")
        html.append(f"<p>Serial: {self.device_info.get('serial','')}<br>Operator: {self.operator.get()}<br>Generated: {now()}</p>")
        html.append(self.report_text.get("1.0","end").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\\n","<br>"))
        html.append("<h3>Test results</h3><table><tr><th>Test</th><th>Result</th><th>Notes</th></tr>")
        for r in rows:html.append("<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(*(str(x).replace("&","&amp;").replace("<","&lt;") for x in r)))
        html.append("</table><p>DEVICEIQ records inspection results; manual tests represent technician-observed outcomes.</p></body></html>")
        Path(p).write_text("".join(html),encoding="utf-8"); self.store.log("REPORT_EXPORTED",p); webbrowser.open(Path(p).as_uri())
    def export_csv(self):
        if not self.inspection_id:return
        p=filedialog.asksaveasfilename(defaultextension=".csv",filetypes=[("CSV","*.csv")],initialfile=f"DEVICEIQ_results_{self.inspection_id}.csv")
        if p:
            with open(p,"w",newline="",encoding="utf-8-sig") as f:
                w=csv.writer(f);w.writerow(["Test","Outcome","Details"])
                for x in self.test_tree.get_children():w.writerow(self.test_tree.item(x,"values"))
    def refresh_history(self):
        for x in self.hist.get_children():self.hist.delete(x)
        for r in self.store.history():self.hist.insert("","end",values=(r["id"],r["started"],f'{r["manufacturer"]} {r["model"]}',r["serial"],r["status"],r["score"],r["grade"],r["operator"]))
    def export_audit(self):
        p=filedialog.asksaveasfilename(defaultextension=".csv",filetypes=[("CSV","*.csv")],initialfile="DEVICEIQ_audit.csv")
        if p:
            rows=self.store.db.execute("SELECT at,action,detail FROM audit ORDER BY id DESC").fetchall()
            with open(p,"w",newline="",encoding="utf-8-sig") as f:
                w=csv.writer(f);w.writerow(["Timestamp","Action","Detail"]);w.writerows([tuple(r) for r in rows])
    def tcp_dialog(self):
        from tkinter.simpledialog import askstring
        host=askstring("ADB over network","Enter phone IP and port (example 192.168.1.20:5555). Enable wireless debugging on the phone first:")
        if host:
            def done(r):messagebox.showinfo("ADB connect",r);self.refresh_devices()
            self.async_task(lambda:self.adb.run(["connect",host]),done)
    def close(self): self.store.close();self.root.destroy()

def main():
    root=tk.Tk(); App(root); root.mainloop()
if __name__=="__main__":main()
