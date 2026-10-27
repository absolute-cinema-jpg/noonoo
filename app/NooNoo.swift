// NooNoo.app - the native window around NooNoo's engine.
//
// Starts `python3 -m noonoo --serve` (bundled in Contents/Resources) as a hidden child process,
// shows its UI in a WKWebView, and handles what a web page can't do well: dragging the XP title bar,
// the size grip, folder pickers, the clipboard, the menu bar and the Dock.
// When the app quits it closes the engine's stdin, which makes the engine pause downloads and exit.

import Cocoa
import UserNotifications
import WebKit

let minSize = NSSize(width: 940, height: 560)
let titleBarHeight: CGFloat = 30      // the XP title bar drawn by the page (#titlebar)
let titleButtonsWidth: CGFloat = 82   // minimise / maximise / close at its right end
let gripSize: CGFloat = 16            // XP size grip, bottom-right corner
let titleIconRect = NSRect(x: 2, y: 2, width: 26, height: 26)   // NooNoo icon in the title bar, measured from top-left

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, UNUserNotificationCenterDelegate,
                         WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow?
    var web: WKWebView!
    var engine: Process?
    var engineStdin: Pipe?
    var xpFrame = true
    var quitting = false
    var overTitleIcon = false
    let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/NooNoo.log")

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        startEngine()
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { granted, error in
            NSLog("NooNoo notifications allowed: %@ %@", granted ? "yes" : "no", error?.localizedDescription ?? "")
        }
    }

    // MARK: notifications ("Download complete") - posted here so they carry NooNoo's name and icon

    func notify(_ json: String) {
        guard let data = json.data(using: .utf8),
              let info = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { return }
        let content = UNMutableNotificationContent()
        content.title = info["title"] ?? "NooNoo"
        content.body = info["text"] ?? ""
        content.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString,
                                                                     content: content, trigger: nil))
    }

    // show banners even while NooNoo is the frontmost app
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .list, .sound])
    }

    // clicking one brings NooNoo forward on the Monitor tab
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        callMenu("monitor")
        done()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window?.makeKeyAndOrderFront(nil)
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        guard let engine = engine, engine.isRunning else { return }
        try? engineStdin?.fileHandleForWriting.close()      // engine pauses jobs, saves, exits
        let deadline = Date().addingTimeInterval(5)
        while engine.isRunning && Date() < deadline { usleep(50_000) }
        if engine.isRunning { engine.terminate() }
    }

    // MARK: engine

    func pythonPath() -> String {
        let candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                          "/Library/Developer/CommandLineTools/usr/bin/python3", "/usr/bin/python3"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
    }

    func startEngine() {
        guard let resources = Bundle.main.resourceURL else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath())
        p.arguments = ["-m", "noonoo", "--serve"]
        p.currentDirectoryURL = resources
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = resources.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["NOONOO_APP"] = Bundle.main.bundlePath      // so the engine can update this copy of the app
        p.environment = env
        let out = Pipe(), stdin = Pipe()
        p.standardOutput = out
        p.standardInput = stdin
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        p.standardError = (try? FileHandle(forWritingTo: logURL)) ?? FileHandle.nullDevice
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.engineStopped(proc.terminationStatus) }
        }

        var buffer = Data()
        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            if chunk.isEmpty { handle.readabilityHandler = nil; return }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: 0x0a) {
                let line = String(decoding: buffer[buffer.startIndex..<nl], as: UTF8.self)
                buffer.removeSubrange(buffer.startIndex...nl)
                if line.hasPrefix("NOONOO_READY ") {
                    let url = String(line.dropFirst("NOONOO_READY ".count))
                    DispatchQueue.main.async { self?.showWindow(url) }
                } else if line == "NOONOO_QUIT" {           // an update is staged; its helper takes over
                    DispatchQueue.main.async { NSApp.terminate(nil) }
                } else if line.hasPrefix("NOONOO_NOTIFY ") {
                    let json = String(line.dropFirst("NOONOO_NOTIFY ".count))
                    DispatchQueue.main.async { self?.notify(json) }
                }
            }
        }
        do {
            try p.run()
            engine = p
            engineStdin = stdin
        } catch {
            fatal("NooNoo couldn't start its engine with \(pythonPath()).\n\n\(error.localizedDescription)")
        }
    }

    func engineStopped(_ status: Int32) {
        if quitting { return }
        fatal("NooNoo's engine stopped unexpectedly (code \(status)).\n\nDetails are in ~/Library/Logs/NooNoo.log")
    }

    func fatal(_ text: String) {
        let alert = NSAlert()
        alert.messageText = "NooNoo"
        alert.informativeText = text
        alert.alertStyle = .critical
        alert.addButton(withTitle: "Quit")
        alert.addButton(withTitle: "Show Log")
        if alert.runModal() == .alertSecondButtonReturn { NSWorkspace.shared.open(logURL) }
        quitting = true
        NSApp.terminate(nil)
    }

    // MARK: window

    func showWindow(_ urlString: String) {
        guard window == nil, let url = URL(string: urlString) else { return }
        xpFrame = urlString.contains("frame=xp")

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "noonoo")
        config.mediaTypesRequiringUserActionForPlayback = []
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")

        var style: NSWindow.StyleMask = [.titled, .closable, .miniaturizable, .resizable]
        if xpFrame { style.insert(.fullSizeContentView) }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 740),
                         styleMask: style, backing: .buffered, defer: false)
        w.title = "NooNoo"
        w.minSize = minSize
        w.delegate = self
        w.isReleasedWhenClosed = false
        w.backgroundColor = NSColor(srgbRed: 0.925, green: 0.914, blue: 0.847, alpha: 1)   // XP "face" colour
        if xpFrame {   // the page draws the XP title bar; hide the Mac one entirely
            w.titlebarAppearsTransparent = true
            w.titleVisibility = .hidden
            for b in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
                w.standardWindowButton(b)?.isHidden = true
            }
        }

        web = WKWebView(frame: w.contentView!.bounds, configuration: config)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        w.contentView!.addSubview(web)

        if !w.setFrameUsingName("NooNoo") { w.center() }
        w.setFrameAutosaveName("NooNoo")
        window = w
        installMouseHandling()
        web.load(URLRequest(url: url))
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Title-bar dragging and the size grip, done natively so the window tracks the cursor exactly.
    func installMouseHandling() {
        // The title bar strip is macOS's, so the page never hears the pointer arrive on NooNoo's icon there.
        window?.acceptsMouseMovedEvents = true
        NSEvent.addLocalMonitorForEvents(matching: .mouseMoved) { [weak self] event in
            guard let self = self, let w = self.window, event.window === w, self.xpFrame,
                  let size = w.contentView?.bounds.size else { return event }
            let p = NSPoint(x: event.locationInWindow.x, y: size.height - event.locationInWindow.y)
            let inside = titleIconRect.contains(p)
            if inside && !self.overTitleIcon {
                self.web.evaluateJavaScript("window.noonooBlink && window.noonooBlink('.tb-icon')", completionHandler: nil)
            }
            self.overTitleIcon = inside
            return event
        }
        NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { [weak self] event in
            guard let self = self, let w = self.window, event.window === w,
                  let size = w.contentView?.bounds.size else { return event }
            let p = event.locationInWindow                       // origin is bottom-left
            let fromTop = size.height - p.y
            let fullScreen = w.styleMask.contains(.fullScreen)
            if self.xpFrame && !fullScreen && fromTop <= titleBarHeight && p.x < size.width - titleButtonsWidth {
                if event.clickCount == 2 {
                    switch UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") {
                    case "Minimize": w.miniaturize(nil)
                    case "None": break
                    default: w.zoom(nil)
                    }
                } else {
                    w.performDrag(with: event)
                }
                return nil
            }
            if !fullScreen && p.x >= size.width - gripSize && p.y <= gripSize {
                self.dragGrip(from: event)
                return nil
            }
            return event
        }
    }

    func dragGrip(from event: NSEvent) {
        guard let w = window else { return }
        let start = NSEvent.mouseLocation, frame0 = w.frame
        w.trackEvents(matching: [.leftMouseDragged, .leftMouseUp], timeout: NSEvent.foreverDuration,
                      mode: .eventTracking) { e, stop in
            guard let e = e else { return }
            if e.type == .leftMouseUp { stop.pointee = true; return }
            let p = NSEvent.mouseLocation
            let width = max(minSize.width, frame0.width + p.x - start.x)
            let height = max(minSize.height, frame0.height - (p.y - start.y))
            w.setFrame(NSRect(x: frame0.minX, y: frame0.maxY - height, width: width, height: height), display: true)
        }
    }

    // MARK: page <-> app

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let action = body["action"] as? String,
              let w = window else { return }
        let id = body["id"] as? Int ?? 0
        switch action {
        case "minimize":
            w.miniaturize(nil); reply(id, nil)
        case "maximize":
            w.zoom(nil); reply(id, nil)
        case "close":
            NSApp.terminate(nil)
        case "clipboard":
            reply(id, NSPasteboard.general.string(forType: .string))
        case "chooseFolder":
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.canCreateDirectories = true
            panel.allowsMultipleSelection = false
            panel.prompt = "Choose"
            panel.message = "Save downloads to:"
            if let start = body["start"] as? String, !start.isEmpty {
                panel.directoryURL = URL(fileURLWithPath: (start as NSString).expandingTildeInPath)
            }
            panel.beginSheetModal(for: w) { [weak self] result in
                self?.reply(id, result == .OK ? panel.url?.path : nil)
            }
        default:
            reply(id, nil)
        }
    }

    func reply(_ id: Int, _ value: String?) {
        var json = "null"
        if let v = value, let data = try? JSONSerialization.data(withJSONObject: [v]),
           let s = String(data: data, encoding: .utf8) {
            json = String(s.dropFirst().dropLast())        // ["..."] -> "..."
        }
        web.evaluateJavaScript("window.__nativeReply(\(id), \(json))", completionHandler: nil)
    }

    func callMenu(_ command: String) {
        web?.evaluateJavaScript("window.noonooMenu && window.noonooMenu('\(command)')", completionHandler: nil)
    }

    // Only NooNoo's own page loads in the window; any other link opens in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.host == "127.0.0.1" || url.scheme == "about" { return decisionHandler(.allow) }
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    // MARK: menu bar

    func buildMenu() {
        let main = NSMenu()

        let app = NSMenu()
        app.addItem(item("About NooNoo", #selector(menuAbout)))
        app.addItem(item("Check for Updates…", #selector(menuUpdates)))
        app.addItem(.separator())
        app.addItem(item("Settings…", #selector(menuOptions), ","))
        app.addItem(.separator())
        app.addItem(item("Hide NooNoo", #selector(NSApplication.hide(_:)), "h"))
        let others = item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h")
        others.keyEquivalentModifierMask = [.command, .option]
        app.addItem(others)
        app.addItem(item("Show All", #selector(NSApplication.unhideAllApplications(_:))))
        app.addItem(.separator())
        app.addItem(item("Quit NooNoo", #selector(NSApplication.terminate(_:)), "q"))
        add(app, to: main)

        // Without these, ⌘C / ⌘V / ⌘A wouldn't reach text fields in the page.
        let edit = NSMenu(title: "Edit")
        edit.addItem(item("Undo", Selector(("undo:")), "z"))
        let redo = item("Redo", Selector(("redo:")), "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(redo)
        edit.addItem(.separator())
        edit.addItem(item("Cut", #selector(NSText.cut(_:)), "x"))
        edit.addItem(item("Copy", #selector(NSText.copy(_:)), "c"))
        edit.addItem(item("Paste", #selector(NSText.paste(_:)), "v"))
        edit.addItem(item("Select All", #selector(NSText.selectAll(_:)), "a"))
        add(edit, to: main)

        let view = NSMenu(title: "View")
        view.addItem(item("Search", #selector(menuSearch)))
        view.addItem(item("Monitor", #selector(menuMonitor)))
        view.addItem(item("Library", #selector(menuLibrary)))
        add(view, to: main)

        let win = NSMenu(title: "Window")
        win.addItem(item("Minimize", #selector(menuMinimize), "m"))
        win.addItem(item("Zoom", #selector(menuZoom)))
        add(win, to: main)
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }

    func item(_ title: String, _ action: Selector, _ key: String = "") -> NSMenuItem {
        NSMenuItem(title: title, action: action, keyEquivalent: key)
    }

    func add(_ menu: NSMenu, to main: NSMenu) {
        let holder = NSMenuItem()
        holder.submenu = menu
        main.addItem(holder)
    }

    @objc func menuAbout() { callMenu("about") }
    @objc func menuUpdates() { callMenu("updates") }
    @objc func menuOptions() { callMenu("options") }
    @objc func menuSearch() { callMenu("search") }
    @objc func menuMonitor() { callMenu("monitor") }
    @objc func menuLibrary() { callMenu("library") }
    @objc func menuMinimize() { window?.miniaturize(nil) }
    @objc func menuZoom() { window?.zoom(nil) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
