// An app-owned Console login. No Chrome/Edge cookie stores are opened or copied.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace QConsole {
public static class ConsoleCreditEntry {
    [STAThread]
    public static void Main(string[] args) {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string sdk = Path.Combine(root, "webview2");
        AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs e) {
            string name = new System.Reflection.AssemblyName(e.Name).Name;
            if (name == "Microsoft.Web.WebView2.Core" || name == "Microsoft.Web.WebView2.WinForms")
                return System.Reflection.Assembly.LoadFrom(Path.Combine(sdk, name + ".dll"));
            return null;
        };
        Environment.SetEnvironmentVariable("PATH", sdk + ";" + Environment.GetEnvironmentVariable("PATH"));
        string home = "", output = "", org = ""; bool connect = false;
        for (int i = 0; i < args.Length; i++) {
            if (args[i] == "-Connect") connect = true;
            else if (args[i] == "-AppHome" && i + 1 < args.Length) home = args[++i];
            else if (args[i] == "-OutputPath" && i + 1 < args.Length) output = args[++i];
            else if (args[i] == "-OrgId" && i + 1 < args.Length) org = args[++i];
        }
        if (home.Length == 0 || output.Length == 0) return;
        try { ConsoleCreditHost.Run(home, output, org, connect); }
        catch { File.WriteAllText(output, "{\"status\":\"unavailable\",\"error\":\"host_error\"}"); }
    }
}
public sealed class ConsoleCreditHost : Form {
    const string Origin = "https://platform.claude.com";
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly string home, output;
    string org, orgName = "", rejectedSession = "", currentSession = "";
    readonly bool connect;
    readonly WebView2 web = new WebView2();
    readonly Label status = new Label();
    readonly System.Windows.Forms.Timer poll = new System.Windows.Forms.Timer();
    readonly System.Windows.Forms.Timer deadline = new System.Windows.Forms.Timer();
    readonly ApplicationContext context;
    readonly List<Form> popups = new List<Form>();
    bool done, polling, loadingApi;

    protected override bool ShowWithoutActivation { get { return !connect; } }

    ConsoleCreditHost(string appHome, string path, string organization, bool login,
                      ApplicationContext appContext) {
        home = appHome; output = path; org = organization; connect = login; context = appContext;
        Text = "Q Console · Claude Console 연결";
        ClientSize = new Size(1100, 780);
        ShowInTaskbar = login;
        if (!login) { Opacity = 0; StartPosition = FormStartPosition.Manual; Location = new Point(-32000, -32000); }
        else {
            StartPosition = FormStartPosition.Manual;
            var area = Screen.PrimaryScreen.WorkingArea;
            Location = new Point(area.Left + Math.Max(0, (area.Width - Width) / 2),
                                 area.Top + Math.Max(0, (area.Height - Height) / 2));
        }
        status.Text = "Claude Platform 개인 계정으로 로그인하세요. API 조직의 잔액만 읽습니다.";
        status.AutoSize = false; status.Height = 38; status.Dock = DockStyle.Top;
        status.Padding = new Padding(12, 10, 0, 0);
        web.Dock = DockStyle.Fill;
        Controls.Add(web); Controls.Add(status);
        FormClosed += delegate { if (!done) Finish("cancelled", null); };
        poll.Interval = 2000;
        poll.Tick += async delegate { await CheckLogin(); };
        deadline.Interval = login ? 900000 : 25000;
        deadline.Tick += delegate { Finish("timeout", null); };
    }

    public static void Run(string home, string output, string org, bool connect) {
        Directory.CreateDirectory(home);
        string suffix;
        using (var sha = SHA256.Create()) {
            suffix = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(Path.GetFullPath(home).ToLowerInvariant()))).Replace("-", "").Substring(0, 16);
        }
        using (var mutex = new Mutex(false, "Local\\q-console-billing-" + suffix)) {
            bool locked = false;
            try {
                try { locked = mutex.WaitOne(connect ? 30000 : 0); }
                catch (AbandonedMutexException) { locked = true; }
                if (!locked) { File.WriteAllText(output, "{\"status\":\"unavailable\",\"error\":\"busy\"}"); return; }
                Application.EnableVisualStyles();
                var ctx = new ApplicationContext();
                using (var host = new ConsoleCreditHost(home, output, org, connect, ctx)) {
                    // The hidden fetch never calls Show/Activate and has no taskbar entry.
                    var unused = host.Handle;
                    var boot = new System.Windows.Forms.Timer();
                    boot.Interval = 1;
                    boot.Tick += async delegate { boot.Stop(); boot.Dispose(); await host.Initialize(); };
                    if (connect) { host.Show(); host.Activate(); host.BringToFront(); }
                    boot.Start(); Application.Run(ctx);
                }
            } finally { if (locked) mutex.ReleaseMutex(); }
        }
    }

    async Task Initialize() {
        try {
            deadline.Start();
            string profile = Path.Combine(home, "claude-console-profile");
            var env = await CoreWebView2Environment.CreateAsync(null, profile, null);
            await web.EnsureCoreWebView2Async(env);
            web.CoreWebView2.Settings.AreDevToolsEnabled = false;
            web.CoreWebView2.Settings.IsStatusBarEnabled = false;
            web.CoreWebView2.NewWindowRequested += async delegate(object sender, CoreWebView2NewWindowRequestedEventArgs e) {
                var deferral = e.GetDeferral();
                try {
                    e.Handled = true;
                    Uri target;
                    if (!connect || !Uri.TryCreate(e.Uri, UriKind.Absolute, out target) || target.Scheme != "https" ||
                        (target.Host != "accounts.google.com" && target.Host != "platform.claude.com")) return;
                    // GIS OAuth needs a real opener, not navigation of the parent.
                    var popup = new Form { Text = "Claude Console · Google 로그인", ClientSize = new Size(680, 760),
                        StartPosition = FormStartPosition.CenterParent };
                    var view = new WebView2 { Dock = DockStyle.Fill };
                    popup.Controls.Add(view); popups.Add(popup);
                    popup.FormClosed += delegate { popups.Remove(popup); view.Dispose(); };
                    popup.Show(this);
                    await view.EnsureCoreWebView2Async(env);
                    view.CoreWebView2.WindowCloseRequested += delegate { popup.Close(); };
                    e.NewWindow = view.CoreWebView2;
                } catch { status.Text = "Google 로그인 창을 열지 못했습니다. 이메일 로그인을 이용하세요."; }
                finally { deferral.Complete(); }
            };
            web.CoreWebView2.NavigationCompleted += async delegate(object sender, CoreWebView2NavigationCompletedEventArgs e) {
                await NavigationDone(e);
            };
            if (connect) { web.CoreWebView2.Navigate(Origin + "/dashboard"); poll.Start(); }
            else if (ValidOrg(org)) { loadingApi = true; web.CoreWebView2.Navigate(CreditUrl()); }
            else Finish("login_required", null);
        } catch { Finish("host_error", null); }
    }

    static bool ValidOrg(string value) { Guid id; return Guid.TryParse(value, out id); }
    string CreditUrl() { return Origin + "/api/organizations/" + org + "/prepaid/credits"; }

    async Task CheckLogin() {
        if (done || polling || loadingApi || web.CoreWebView2 == null) return;
        polling = true;
        try {
            var cookies = await web.CoreWebView2.CookieManager.GetCookiesAsync(Origin);
            var session = cookies.FirstOrDefault(c => c.Name == "sessionKey");
            // Own-profile cookies stay only inside WebView2 and this process's memory.
            if (session != null && session.Value != rejectedSession) {
                currentSession = session.Value;
                loadingApi = true;
                web.CoreWebView2.Navigate(Origin + "/api/organizations");
            }
        } catch { status.Text = "로그인을 확인하는 중입니다. Console 로그인을 완료하세요."; }
        finally { polling = false; }
    }

    async Task NavigationDone(CoreWebView2NavigationCompletedEventArgs e) {
        if (done) return;
        try {
            Uri uri;
            if (!Uri.TryCreate(web.Source.ToString(), UriKind.Absolute, out uri) || uri.Host != "platform.claude.com") return;
            if (!uri.AbsolutePath.StartsWith("/api/organizations")) {
                if (!connect && loadingApi) Finish("auth", null);
                return;
            }
            if (!e.IsSuccess) { Finish("network", null); return; }
            int code = e.HttpStatusCode;
            if (code == 401 || code == 403) {
                if (connect) {
                    rejectedSession = currentSession; loadingApi = false;
                    status.Text = "이 Console 세션으로 조회할 수 없습니다. 개인 계정으로 다시 로그인하세요.";
                    web.CoreWebView2.Navigate(Origin + "/dashboard");
                } else Finish(code == 401 ? "auth" : "forbidden", null);
                return;
            }
            if (code == 429) { Finish("rate_limit", null); return; }
            if (code != 200) { Finish("network", null); return; }
            string encoded = await web.CoreWebView2.ExecuteScriptAsync("document.body ? document.body.innerText : ''");
            string body = json.Deserialize<string>(encoded);
            if (uri.AbsolutePath.TrimEnd('/') == "/api/organizations") {
                var list = json.DeserializeObject(body) as object[];
                if (list == null) { Finish("invalid", null); return; }
                var candidates = list.OfType<Dictionary<string, object>>().Where(o => {
                    object caps; return o.TryGetValue("capabilities", out caps) &&
                        (caps as object[] ?? new object[0]).Any(c => Convert.ToString(c) == "api");
                }).ToList();
                if (candidates.Count == 0) { Finish("no_api_org", null); return; }
                Dictionary<string, object> chosen = null;
                if (ValidOrg(org)) chosen = candidates.FirstOrDefault(o => Convert.ToString(o["uuid"]) == org);
                else if (candidates.Count == 1) chosen = candidates[0];
                else chosen = SelectOrganization(candidates);
                if (chosen == null) { Finish(ValidOrg(org) ? "org_mismatch" : "cancelled", null); return; }
                org = Convert.ToString(chosen["uuid"]); orgName = Convert.ToString(chosen["name"]);
                if (!ValidOrg(org)) { Finish("invalid", null); return; }
                status.Text = orgName + " · 조직 전체 잔액 확인 중";
                web.CoreWebView2.Navigate(CreditUrl());
                return;
            }
            if (uri.AbsolutePath != new Uri(CreditUrl()).AbsolutePath) { Finish("invalid", null); return; }
            var payload = json.DeserializeObject(body) as Dictionary<string, object>;
            if (payload == null || !payload.ContainsKey("amount") || !payload.ContainsKey("balance")) { Finish("invalid", null); return; }
            var safe = new Dictionary<string, object>();
            foreach (string key in new [] { "amount", "balance", "currency", "tranches", "promo_tranches", "next_expires_at" }) {
                if (payload.ContainsKey(key)) safe[key] = payload[key];
            }
            if (connect) {
                var metadata = new Dictionary<string, object> { { "org_id", org }, { "org_name", orgName } };
                AtomicWrite(Path.Combine(home, "claude-console-connection.json"), json.Serialize(metadata));
            }
            Finish(null, safe);
        } catch { Finish("invalid", null); }
    }

    Dictionary<string, object> SelectOrganization(List<Dictionary<string, object>> list) {
        using (var picker = new Form()) {
            picker.Text = "잔액을 표시할 API 조직"; picker.ClientSize = new Size(460, 100);
            picker.StartPosition = FormStartPosition.CenterParent;
            var options = new ComboBox { Dock = DockStyle.Top, DropDownStyle = ComboBoxStyle.DropDownList };
            foreach (var item in list) options.Items.Add(Convert.ToString(item["name"]));
            options.SelectedIndex = 0;
            var ok = new Button { Text = "연결", Dock = DockStyle.Bottom, DialogResult = DialogResult.OK };
            picker.Controls.Add(options); picker.Controls.Add(ok); picker.AcceptButton = ok;
            return picker.ShowDialog(this) == DialogResult.OK ? list[options.SelectedIndex] : null;
        }
    }

    static void AtomicWrite(string path, string content) {
        string tmp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        File.WriteAllText(tmp, content, new UTF8Encoding(false));
        if (File.Exists(path)) File.Replace(tmp, path, null); else File.Move(tmp, path);
    }

    void Finish(string error, object payload) {
        if (done) return; done = true; poll.Stop(); deadline.Stop();
        var result = new Dictionary<string, object> { { "status", error == null ? "ok" : "unavailable" },
            { "org_id", org }, { "org_name", orgName },
            { "received_at", (long)(DateTime.UtcNow - new DateTime(1970,1,1)).TotalSeconds } };
        if (error != null) result["error"] = error; else result["payload"] = payload;
        try { AtomicWrite(output, json.Serialize(result)); } catch { }
        foreach (var popup in popups.ToArray()) popup.Close();
        web.Dispose(); Close(); context.ExitThread();
    }
}
}
