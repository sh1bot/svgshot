import Foundation
import AppKit
import ApplicationServices
import ScreenCaptureKit
import ImageIO
import UniformTypeIdentifiers
import CZlib

#if !SVGSHOT_BUILD_VERSION
let captureBuildCommit = "unknown"
#endif

func fail(_ message: String) -> NSError { NSError(domain: "svgshot", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
func status(_ s: String, _ value: Any? = nil) -> [String: Any] {
    var r: [String: Any] = ["status": s]
    if s == "value", let value { r["value"] = value }
    return r
}
func attribute(_ e: AXUIElement, _ name: String) -> (AnyObject?, AXError) {
    var result: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(e, name as CFString, &result)
    return (result, error)
}
func parameter(_ e: AXUIElement, _ name: String, _ value: CFTypeRef) -> CFTypeRef? {
    var result: CFTypeRef?
    return AXUIElementCopyParameterizedAttributeValue(e, name as CFString, value, &result) == .success ? result : nil
}
func bounds(_ e: AXUIElement) -> CGRect? {
    let (p, pe) = attribute(e, kAXPositionAttribute), (s, se) = attribute(e, kAXSizeAttribute)
    guard pe == .success, se == .success, let p, let s,
          CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point), AXValueGetValue(s as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}
func rectangle(_ r: CGRect, _ origin: CGRect, _ width: Int, _ height: Int) -> [Double] {
    let sx = Double(width)/origin.width, sy = Double(height)/origin.height
    return [(r.minX-origin.minX)*sx, (r.minY-origin.minY)*sy, max(0,r.width*sx), max(0,r.height*sy)]
}
let roles: [String:String] = ["AXButton":"button", "AXCheckBox":"checkbox", "AXRadioButton":"radiobutton",
 "AXTextField":"edit", "AXTextArea":"edit", "AXStaticText":"text", "AXWindow":"window",
 "AXGroup":"group", "AXImage":"image", "AXLink":"hyperlink", "AXList":"list", "AXTable":"table",
 "AXRow":"dataitem", "AXCell":"dataitem", "AXColumn":"group", "AXOutline":"tree", "AXComboBox":"combobox",
 "AXPopUpButton":"combobox", "AXMenu":"menu", "AXMenuItem":"menuitem", "AXMenuBar":"menubar",
 "AXScrollBar":"scrollbar", "AXSlider":"slider", "AXTabGroup":"tab", "AXToolbar":"toolbar",
 "AXProgressIndicator":"progressbar", "AXSplitGroup":"group", "AXScrollArea":"pane"]

final class Reader {
    let viewport: CGRect, width: Int, height: Int
    let debugUnredacted: Bool
    var nodes: [String: Any] = [:], elements: [AXUIElement] = [], warnings: [String] = []
    let started = Date()
    init(_ viewport: CGRect, _ width: Int, _ height: Int, _ debug: Bool) { self.viewport=viewport; self.width=width; self.height=height; self.debugUnredacted=debug }
    func id(_ e: AXUIElement) -> String {
        if let i=elements.firstIndex(where:{CFEqual($0,e)}) {return "n\(i+1)"}
        elements.append(e);return "n\(elements.count)"
    }
    func text(_ e: AXUIElement) -> [String: Any] {
        let (v, error) = attribute(e, kAXVisibleCharacterRangeAttribute)
        guard error == .success, let v, CFGetTypeID(v)==AXValueGetTypeID() else {
            return ["status": error == .attributeUnsupported ? "not_supported":"error", "lines": []]
        }
        var visible=CFRange()
        guard AXValueGetValue(v as! AXValue,.cfRange,&visible), visible.location>=0, visible.length>=0 else {
            return ["status":"error","lines":[]]
        }
        var lines: [[String: Any]] = [], offset=visible.location
        let end=visible.location+min(visible.length,65536)
        if visible.length>65536 { warnings.append("Visible text truncated at 65536 UTF-16 code units") }
        while offset<end && lines.count<2000 {
            let index=offset
            let cfIndex=NSNumber(value:index)
            let lineNumber=parameter(e,kAXLineForIndexParameterizedAttribute,cfIndex)
            var range=CFRange(location:offset,length:end-offset)
            if let lineNumber, let lineRange=parameter(e,kAXRangeForLineParameterizedAttribute,lineNumber), CFGetTypeID(lineRange)==AXValueGetTypeID() {
                var full=CFRange()
                if AXValueGetValue(lineRange as! AXValue,.cfRange,&full) {
                    range=CFRange(location:offset,length:max(0,min(end,full.location+full.length)-offset))
                }
            }
            guard range.length>0, let axRange=AXValueCreate(.cfRange,&range),
                  let str=parameter(e,kAXStringForRangeParameterizedAttribute,axRange) as? String,
                  let rectValue=parameter(e,kAXBoundsForRangeParameterizedAttribute,axRange), CFGetTypeID(rectValue)==AXValueGetTypeID() else { break }
            var r=CGRect.zero
            guard AXValueGetValue(rectValue as! AXValue,.cgRect,&r), viewport.intersects(r) else { break }
            var line: [String:Any] = ["content":str,"rectangles":[rectangle(r,viewport,width,height)],"style":[:],"runs":[]]
            if let a=parameter(e,kAXAttributedStringForRangeParameterizedAttribute,axRange), CFGetTypeID(a)==CFAttributedStringGetTypeID() {
                let attributed=a as! CFAttributedString
                var pos=0, runs: [[String:Any]]=[]
                while pos<CFAttributedStringGetLength(attributed) && runs.count<2048 {
                    var effective=CFRange()
                    let attrs=CFAttributedStringGetAttributes(attributed,pos,&effective) as NSDictionary
                    guard effective.length>0 else { break }
                    var style: [String:Any]=[:]
                    if let font=attrs[kAXFontTextAttribute] as? NSDictionary {
                        style["font_family"]=font[kAXFontFamilyKey] as? String
                        style["font_size"]=font[kAXFontSizeKey] as? NSNumber
                        style["font_size_unit"]="pt"
                    }
                    if let color=attrs[kAXForegroundColorTextAttribute], CFGetTypeID(color as CFTypeRef)==CGColor.typeID {
                        let c=color as! CGColor
                        if let rgb=c.converted(to:CGColorSpace(name:CGColorSpace.sRGB)!,intent:.defaultIntent,options:nil), let parts=rgb.components, parts.count>=3 {
                            style["foreground"]=String(format:"#%02x%02x%02x",Int((parts[0]*255).rounded()),Int((parts[1]*255).rounded()),Int((parts[2]*255).rounded()))
                        }
                    }
                    let ns=str as NSString
                    let length=min(effective.length,ns.length-pos)
                    guard length>0 else { break }
                    runs.append(["content":ns.substring(with:NSRange(location:pos,length:length)),"rectangles":[],"style":style])
                    pos += length
                }
                line["runs"]=runs
            }
            lines.append(line);offset += range.length
        }
        if offset<end { warnings.append("Text capture incomplete: provider geometry or line limit") }
        return ["status":offset==end ? "value":"truncated","lines":lines,"selections":status("not_captured")]
    }
    func node(_ e: AXUIElement, _ depth: Int = 0) -> [String:Any]? {
        if nodes.count>=5000 || depth>64 || Date().timeIntervalSince(started)>20 {
            warnings.append("AX traversal truncated by count, depth or time limit");return nil
        }
        let key=id(e)
        if nodes[key] != nil { return nil }
        nodes[key]=[:]
        var raw: [String:Any]=[:], fieldStatus: [String:Any]=[:]
        var attributeNames: CFArray?
        if AXUIElementCopyAttributeNames(e,&attributeNames) == .success, let names=attributeNames as? [String] {
            for name in names {raw[name]=status("not_captured")}
        }
        func get(_ name: String) -> AnyObject? {
            let (value,error)=attribute(e,name)
            if error != .success { raw[name]=status(error == .attributeUnsupported ? "not_supported":"error"); return nil }
            if let v=value, v is String || v is NSNumber { raw[name]=status("value",v) }
            return value
        }
        let role=get(kAXRoleAttribute) as? String ?? "custom"
        let subrole=get(kAXSubroleAttribute) as? String
        let protected=role == "custom" || subrole == kAXSecureTextFieldSubrole || (role == kAXTextFieldRole && subrole == nil)
        let b=bounds(e)
        let explicitHidden=attribute(e,"AXHidden").0 as? NSNumber
        let hidden=b == nil || !viewport.intersects(b!) || explicitHidden?.boolValue == true
        let redacted=(protected || hidden) && !debugUnredacted
        if b == nil || b!.width==0 || b!.height==0 {fieldStatus["bounds"]=status("error")}
        var states: [String:Any]=["protected":protected,"offscreen":hidden]
        for (attr,dest) in [(kAXEnabledAttribute,"enabled"),(kAXFocusedAttribute,"focused"),(kAXSelectedAttribute,"selected")] {
            if let v=get(attr) as? NSNumber { states[dest]=v.boolValue }
            else { fieldStatus["states."+dest]=raw[attr] }
        }
        var out: [String:Any]=["id":key,"role":roles[role] ?? "custom","bounds":rectangle(b ?? .zero,viewport,width,height),
          "states":states,"relationships":[:],"children":[],"native_ref":key,
          "text":["status":redacted ? "redacted":"not_supported","lines":[]],"value":status(redacted ? "redacted":"not_captured")]
        if !redacted {
            for (attr,dest) in [(kAXTitleAttribute,"label"),(kAXDescriptionAttribute,"description"),(kAXHelpAttribute,"help"),(kAXRoleDescriptionAttribute,"role_description")] {
                if let v=get(attr) as? String { out[dest]=String(v.prefix(65536)) }
            }
            if role == kAXStaticTextRole, let value=get(kAXValueAttribute) as? String { out["label"]=String(value.prefix(65536)) }
            if debugUnredacted, let value=get(kAXValueAttribute) {
                if let string=value as? String { out["value"]=status("value",String(string.prefix(65536))) }
                else if let number=value as? NSNumber { out["value"]=status("value",number) }
            }
            if [kAXCheckBoxRole,kAXRadioButtonRole].contains(role), let v=get(kAXValueAttribute) as? NSNumber {
                states["checked"]=v.intValue==0 ? "unchecked":v.intValue==1 ? "checked":"mixed"
            }
            if let expanded=get(kAXExpandedAttribute) as? NSNumber { states["expansion"]=expanded.boolValue ? "expanded":"collapsed" }
            out["text"]=text(e)
            if let labels=attribute(e,kAXTitleUIElementAttribute).0, CFGetTypeID(labels)==AXUIElementGetTypeID() {
                out["relationships"]=["labelled_by":[id(labels as! AXUIElement)]]
            }
        }
        out["states"]=states
        if !fieldStatus.isEmpty { out["field_status"]=fieldStatus }
        if (!protected || debugUnredacted), let children=attribute(e,kAXChildrenAttribute).0 as? [AXUIElement] {
            out["children"]=children.prefix(5000).compactMap { node($0,depth+1) }
        }
        nodes[key]=raw;return out
    }
}
func be32(_ value: UInt32) -> Data { var v=value.bigEndian;return withUnsafeBytes(of:&v){Data($0)} }
func embedded(_ png: Data, _ json: Data) throws -> Data {
    var compressed=Data(count:Int(compressBound(uLong(json.count)))), size=uLongf(compressed.count)
    let result=compressed.withUnsafeMutableBytes { dst in json.withUnsafeBytes { src in
        compress2(dst.bindMemory(to:Bytef.self).baseAddress!,&size,src.bindMemory(to:Bytef.self).baseAddress!,uLong(json.count),9)
    }}
    guard result==Z_OK, json.count<=64*1024*1024 else { throw fail("Snapshot compression failed or exceeds limit") }
    compressed.count=Int(size)
    var payload=Data([83,86,71,83,72,79,84,0,1,1,1,0]);payload.append(be32(UInt32(json.count)));payload.append(compressed)
    guard payload.count<=16*1024*1024 else {throw fail("Compressed snapshot exceeds limit")}
    var body=Data("seMA".utf8);body.append(payload)
    let crc=body.withUnsafeBytes { crc32(0,$0.bindMemory(to:Bytef.self).baseAddress,uInt(body.count)) }
    var chunk=be32(UInt32(payload.count));chunk.append(body);chunk.append(be32(UInt32(crc)))
    var out=png;out.insert(contentsOf:chunk,at:png.count-12);return out
}

func desktopCaptureURL(_ title: String) throws -> URL {
    guard let desktop=FileManager.default.urls(for:.desktopDirectory,in:.userDomainMask).first else {
        throw fail("Cannot locate Desktop folder")
    }
    try FileManager.default.createDirectory(at:desktop,withIntermediateDirectories:true)
    var clean=String(title.prefix(100)).map { c -> Character in
        if c.unicodeScalars.contains(where: {$0.value<32}) || "<>:\"/\\|?*".contains(c) {return "_"}
        return c
    }
    while String(clean).utf8.count>180 {clean.removeLast()}
    while let last=clean.last,last=="." || last==" " {clean.removeLast()}
    let name=clean.isEmpty ? "Window" : String(clean)
    let formatter=DateFormatter()
    formatter.locale=Locale(identifier:"en_US_POSIX")
    formatter.calendar=Calendar(identifier:.gregorian)
    formatter.timeZone=TimeZone.current
    formatter.dateFormat="yyyy-MM-dd_HH-mm-ss-SSS"
    let stem="\(formatter.string(from:Date())) - \(name)"
    var output=desktop.appendingPathComponent(stem+".png")
    var suffix=2
    while FileManager.default.fileExists(atPath:output.path) {
        output=desktop.appendingPathComponent("\(stem) (\(suffix)).png");suffix+=1
    }
    return output
}
@main struct Main {
    @MainActor static func main() async {
        do {
            let args=Array(CommandLine.arguments.dropFirst())
            if args == ["--version"] {
                print("svgshot-capture-macos \(captureBuildCommit)");return
            }
            if args.contains("--help") {
                print("svgshot-capture-macos [capture.png | --out capture.png | --stdout] [--window CGWindowID | --foreground] [--delay SECONDS] [--list-windows] [--debug-unredacted]\nWithout a filename, save a dated PNG on the Desktop.");return
            }
            var values:[String:String]=[:],flags=Set<String>(),filename:String?
            var i=0
            while i<args.count {
                let arg=args[i]
                if ["--out","--window","--delay"].contains(arg) {
                    guard i+1<args.count else {throw fail("Incomplete option \(arg)")}
                    i+=1;values[arg]=args[i]
                } else if ["--foreground","--stdout","--framed","--list-windows","--debug-unredacted"].contains(arg) {
                    flags.insert(arg)
                } else if !arg.hasPrefix("-"),filename==nil {filename=arg}
                else {throw fail("Unknown or duplicate argument \(arg)")}
                i+=1
            }
            guard filename==nil || values["--out"]==nil else {throw fail("Choose a filename or --out, not both")}
            filename=filename ?? values["--out"]
            if let filename,URL(fileURLWithPath:filename).pathExtension.lowercased() != "png" {
                throw fail("Output filename must end in .png")
            }
            let binary=flags.contains("--stdout") || flags.contains("--framed")
            guard !binary || filename==nil else {throw fail("Binary output cannot be combined with an output filename")}
            guard !flags.contains("--stdout") || !flags.contains("--framed") else {throw fail("Choose --stdout or --framed")}
            guard values["--window"]==nil || !flags.contains("--foreground") else {throw fail("Choose --window or --foreground")}
            if let id=values["--window"],UInt32(id)==nil {throw fail("Invalid window ID")}
            func option(_ key: String) -> String? {return values[key]}
            let delay=Double(option("--delay") ?? "0") ?? -1
            guard delay>=0 && delay<=60 else { throw fail("Delay must be 0–60") }
            let debugUnredacted=flags.contains("--debug-unredacted")
            if debugUnredacted { fputs("WARNING: debug unredacted capture may include sensitive information, including passwords and offscreen content.\n",stderr) }
            if delay>0 { try await Task.sleep(nanoseconds:UInt64(delay*1_000_000_000)) }
            let content=try await SCShareableContent.excludingDesktopWindows(true,onScreenWindowsOnly:true)
            let windows=content.windows.filter { $0.frame.width>0 && $0.frame.height>0 && $0.owningApplication?.processID != getpid() }
            if args.contains("--list-windows") {
                let list=windows.map { ["id":String($0.windowID),"title":$0.title ?? "","application":$0.owningApplication?.applicationName ?? ""] }
                FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject:list,options:.prettyPrinted));return
            }
            guard AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String:true] as CFDictionary) else {
                throw fail("Grant Accessibility access in System Settings and run again")
            }
            var chosen: SCWindow?
            if let id=option("--window"),let number=UInt32(id) { chosen=windows.first{$0.windowID==number} }
            else if args.contains("--foreground") {
                let pid=NSWorkspace.shared.frontmostApplication?.processIdentifier
                let app=pid.map{AXUIElementCreateApplication($0)}
                let focused=app.flatMap{attribute($0,kAXFocusedWindowAttribute).0} as! AXUIElement?
                let frame=focused.flatMap{bounds($0)}
                let matches=windows.filter{$0.owningApplication?.processID==pid && frame != nil && $0.frame == frame!}
                if matches.count==1 {chosen=matches[0]}
            } else {
                NSApplication.shared.setActivationPolicy(.accessory)
                let alert=NSAlert();alert.messageText="Choose a window to capture";alert.addButton(withTitle:"Capture");alert.addButton(withTitle:"Cancel")
                let menu=NSPopUpButton(frame:NSRect(x:0,y:0,width:500,height:30))
                for w in windows {menu.addItem(withTitle:"\(w.owningApplication?.applicationName ?? "App") — \(w.title ?? "Untitled") [\(w.windowID)]")}
                alert.accessoryView=menu;NSApplication.shared.activate(ignoringOtherApps:true)
                guard alert.runModal() == .alertFirstButtonReturn,menu.indexOfSelectedItem>=0 else {throw fail("Capture cancelled")}
                chosen=windows[menu.indexOfSelectedItem]
            }
            guard let window=chosen,let owner=window.owningApplication else {throw fail("Target window missing or ambiguous")}
            let app=AXUIElementCreateApplication(owner.processID)
            AXUIElementSetMessagingTimeout(app,1)
            let axWindows=attribute(app,kAXWindowsAttribute).0 as? [AXUIElement] ?? []
            let matches=axWindows.filter{e in guard let b=bounds(e) else{return false};return abs(b.minX-window.frame.minX)<2 && abs(b.minY-window.frame.minY)<2 && abs(b.width-window.frame.width)<2 && abs(b.height-window.frame.height)<2}
            guard matches.count==1, let target=matches.first,let before=bounds(target) else {throw fail("Cannot uniquely associate screenshot window with AX window")}
            let filter=SCContentFilter(desktopIndependentWindow:window), config=SCStreamConfiguration()
            let width=Int((window.frame.width*Double(filter.pointPixelScale)).rounded()), height=Int((window.frame.height*Double(filter.pointPixelScale)).rounded())
            config.width=width;config.height=height;config.showsCursor=false;config.ignoreShadowsSingleWindow=true
            let reader=Reader(before,width,height,debugUnredacted)
            if debugUnredacted { reader.warnings.append("Debug unredacted capture may include sensitive information, including passwords and offscreen content.") }
            guard var root=reader.node(target) else {throw fail("Accessibility tree unavailable")}
            // Remove relationships to nodes outside this capture; native refs remain local metadata.
            func prune(_ n: inout [String:Any]) {
                if let rel=n["relationships"] as? [String:[String]] {n["relationships"]=rel.mapValues{$0.filter{reader.nodes[$0] != nil}}}
                var children=n["children"] as? [[String:Any]] ?? []
                for i in children.indices {prune(&children[i])};n["children"]=children
            };prune(&root)
            let image=try await SCScreenshotManager.captureImage(contentFilter:filter,configuration:config)
            guard bounds(target)==before,image.width==width,image.height==height else {throw fail("Window moved or changed size during capture")}
            let bytes=NSMutableData()
            guard let dest=CGImageDestinationCreateWithData(bytes,UTType.png.identifier as CFString,1,nil) else {throw fail("PNG encoder unavailable")}
            CGImageDestinationAddImage(dest,image,nil);guard CGImageDestinationFinalize(dest) else {throw fail("PNG encoding failed")}
            let sx=Double(width)/before.width,sy=Double(height)/before.height
            let snapshot:[String:Any]=["format":"svgshot.capture","version":3,"source":["platform":"macos","provider":"macos-ax"],
              "image":["size":[width,height],"coordinate_space":"image-pixels","source_bounds":[before.minX,before.minY,before.width,before.height],"source_to_image":[sx,0,0,sy,-before.minX*sx,-before.minY*sy]],
              "root":root,"native":["provider":"macos-ax","nodes":reader.nodes],"warnings":reader.warnings,
              "capture_policy":["include_hidden_content":debugUnredacted,"debug_unredacted":debugUnredacted,"password_content":debugUnredacted ? "included":"redacted","actions_invoked":false]]
            let json=try JSONSerialization.data(withJSONObject:snapshot,options:.sortedKeys),png=bytes as Data
            if args.contains("--framed") {
                FileHandle.standardOutput.write(be32(UInt32(json.count)));FileHandle.standardOutput.write(json);FileHandle.standardOutput.write(png)
            } else {
                let capture=try embedded(png,json)
                if flags.contains("--stdout") {FileHandle.standardOutput.write(capture)}
                else {
                    let output=try filename.map{URL(fileURLWithPath:$0)} ?? desktopCaptureURL(window.title ?? "")
                    try capture.write(to:output,options:filename==nil ? .withoutOverwriting : .atomic)
                    print(output.path)
                }
            }
        } catch {
            FileHandle.standardError.write(Data("svgshot capture: \(error.localizedDescription)\n".utf8));exit(1)
        }
    }
}
