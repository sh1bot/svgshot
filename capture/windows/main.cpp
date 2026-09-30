// Windows SDK only: UI Automation + Windows Graphics Capture + WIC PNG.
#include <windows.h>
#include <UIAutomation.h>
#include <dwmapi.h>
#include <d3d11.h>
#include <wincodec.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
using winrt::com_ptr;
using winrt::check_hresult;
namespace capture = winrt::Windows::Graphics::Capture;

std::string utf8(const std::wstring& s) {
    if (s.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0, nullptr, nullptr);
    std::string out(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), out.data(), n, nullptr, nullptr);
    return out;
}
std::string json(const std::wstring& s) {
    std::string out = "\"";
    for (unsigned char c : utf8(s)) {
        if (c == '"' || c == '\\') { out += '\\'; out += c; }
        else if (c < 32) { char b[7]; sprintf_s(b, "\\u%04x", c); out += b; }
        else out += c;
    }
    return out + '"';
}
std::wstring bstr(BSTR value) {
    std::wstring out = value ? std::wstring(value, SysStringLen(value)) : L"";
    SysFreeString(value); return out;
}
std::string rect_json(RECT r) {
    return "[" + std::to_string(r.left) + "," + std::to_string(r.top) + "," +
        std::to_string(r.right-r.left) + "," + std::to_string(r.bottom-r.top) + "]";
}
RECT bounds(HWND hwnd) {
    RECT r{};
    if (FAILED(DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, &r, sizeof(r))))
        if (!GetWindowRect(hwnd, &r)) throw std::runtime_error("Cannot read window bounds");
    return r;
}
std::string runtime_id(IUIAutomationElement* e) {
    SAFEARRAY* a = nullptr;
    if (FAILED(e->GetRuntimeId(&a)) || !a) return {};
    LONG low = 0, high = -1; SafeArrayGetLBound(a, 1, &low); SafeArrayGetUBound(a, 1, &high);
    std::string result = "uia";
    for (LONG i=low; i<=high; ++i) { int value=0; SafeArrayGetElement(a, &i, &value); result += "-"+std::to_string(value); }
    SafeArrayDestroy(a); return result;
}
template<typename T> com_ptr<T> pattern(IUIAutomationElement* e, PATTERNID id) {
    com_ptr<T> p; e->GetCurrentPatternAs(id, __uuidof(T), p.put_void()); return p;
}
std::string text_ranges(IUIAutomationElement* e) {
    auto p = pattern<IUIAutomationTextPattern>(e, UIA_TextPatternId);
    if (!p) return "[]";
    com_ptr<IUIAutomationTextRangeArray> ranges;
    if (FAILED(p->GetVisibleRanges(ranges.put())) || !ranges) return "[]";
    int n = 0; ranges->get_Length(&n);
    std::ostringstream out; out << '['; bool comma = false; int budget=2000;
    for (int i=0; i<n && budget>0; ++i) {
        com_ptr<IUIAutomationTextRange> visible, line;
        if (FAILED(ranges->GetElement(i, visible.put())) || !visible) continue;
        if (FAILED(visible->Clone(line.put())) || !line) continue;
        line->MoveEndpointByRange(TextPatternRangeEndpoint_End, visible.get(), TextPatternRangeEndpoint_Start);
        line->ExpandToEnclosingUnit(TextUnit_Line);
        while (budget-- > 0) {
            int cmp=0; line->CompareEndpoints(TextPatternRangeEndpoint_Start, visible.get(), TextPatternRangeEndpoint_End, &cmp);
            if (cmp>=0) break;
            com_ptr<IUIAutomationTextRange> clipped;
            if (FAILED(line->Clone(clipped.put()))) break;
            clipped->CompareEndpoints(TextPatternRangeEndpoint_Start, visible.get(), TextPatternRangeEndpoint_Start, &cmp);
            if (cmp<0) clipped->MoveEndpointByRange(TextPatternRangeEndpoint_Start, visible.get(), TextPatternRangeEndpoint_Start);
            clipped->CompareEndpoints(TextPatternRangeEndpoint_End, visible.get(), TextPatternRangeEndpoint_End, &cmp);
            if (cmp>0) clipped->MoveEndpointByRange(TextPatternRangeEndpoint_End, visible.get(), TextPatternRangeEndpoint_End);
            BSTR raw=nullptr; clipped->GetText(-1, &raw); auto text=bstr(raw);
            while (!text.empty() && (text.back()==L'\r' || text.back()==L'\n')) text.pop_back();
            SAFEARRAY* a=nullptr;
            if (!text.empty() && SUCCEEDED(clipped->GetBoundingRectangles(&a)) && a) {
                LONG low=0, high=-1; SafeArrayGetLBound(a,1,&low); SafeArrayGetUBound(a,1,&high);
                if (high-low+1>=4) {
                    if (comma) out << ','; comma=true;
                    out << "{\"text\":" << json(text) << ",\"rectangles\":[";
                    for (LONG j=low; j+3<=high; j+=4) {
                        if (j>low) out << ',';
                        out << '[';
                        for (LONG k=j; k<j+4; ++k) { double v=0; SafeArrayGetElement(a,&k,&v); if (k>j) out << ','; out << v; }
                        out << ']';
                    }
                    out << "]}";
                }
            }
            if (a) SafeArrayDestroy(a);
            int moved=0; if (FAILED(line->Move(TextUnit_Line,1,&moved)) || moved==0) break;
        }
    }
    out << ']'; return out.str();
}
struct Reader {
    com_ptr<IUIAutomation> automation;
    com_ptr<IUIAutomationTreeWalker> walker;
    int count=0; bool truncated=false;
    Reader() {
        check_hresult(CoCreateInstance(__uuidof(CUIAutomation),nullptr,CLSCTX_INPROC_SERVER,IID_PPV_ARGS(automation.put())));
        check_hresult(automation->get_RawViewWalker(walker.put()));
    }
    std::string node(IUIAutomationElement* e, int depth=0) {
        if (++count>5000 || depth>64) { truncated=true; return "null"; }
        RECT r{}; e->get_CurrentBoundingRectangle(&r);
        CONTROLTYPEID type=0; e->get_CurrentControlType(&type);
        BOOL password=FALSE; e->get_CurrentIsPassword(&password);
        std::ostringstream out;
        out << "{\"id\":\"" << runtime_id(e) << "\",\"control_type\":" << type << ",\"bounds\":" << rect_json(r);
        auto str = [&](const char* key, auto getter) { BSTR value=nullptr; (e->*getter)(&value); out << ",\"" << key << "\":" << json(bstr(value)); };
        str("name", &IUIAutomationElement::get_CurrentName);
        str("localized_control_type", &IUIAutomationElement::get_CurrentLocalizedControlType);
        str("automation_id", &IUIAutomationElement::get_CurrentAutomationId);
        str("class_name", &IUIAutomationElement::get_CurrentClassName);
        str("framework_id", &IUIAutomationElement::get_CurrentFrameworkId);
        str("help_text", &IUIAutomationElement::get_CurrentHelpText);
        str("access_key", &IUIAutomationElement::get_CurrentAccessKey);
        str("accelerator_key", &IUIAutomationElement::get_CurrentAcceleratorKey);
        auto flag = [&](const char* key, auto getter) { BOOL value=FALSE; (e->*getter)(&value); out << ",\"" << key << "\":" << (value ? "true":"false"); };
        flag("enabled", &IUIAutomationElement::get_CurrentIsEnabled);
        flag("offscreen", &IUIAutomationElement::get_CurrentIsOffscreen);
        flag("keyboard_focus", &IUIAutomationElement::get_CurrentHasKeyboardFocus);
        flag("focusable", &IUIAutomationElement::get_CurrentIsKeyboardFocusable);
        flag("control_element", &IUIAutomationElement::get_CurrentIsControlElement);
        flag("content_element", &IUIAutomationElement::get_CurrentIsContentElement);
        out << ",\"password\":" << (password ? "true":"false");
        com_ptr<IUIAutomationElement> label; e->get_CurrentLabeledBy(label.put());
        if (label) out << ",\"labeled_by\":\"" << runtime_id(label.get()) << '"';
        out << ",\"states\":{"; bool state=false;
        auto state_key = [&](const char* key) { if (state) out << ','; state=true; out << '"' << key << "\":"; };
        if (auto p=pattern<IUIAutomationTogglePattern>(e, UIA_TogglePatternId)) {
            ToggleState value=ToggleState_Off; if (SUCCEEDED(p->get_CurrentToggleState(&value))) { state_key("toggle"); out << static_cast<int>(value); }
        }
        if (auto p=pattern<IUIAutomationSelectionItemPattern>(e, UIA_SelectionItemPatternId)) {
            BOOL value=FALSE; if (SUCCEEDED(p->get_CurrentIsSelected(&value))) { state_key("selected"); out << (value?"true":"false"); }
        }
        if (auto p=pattern<IUIAutomationExpandCollapsePattern>(e, UIA_ExpandCollapsePatternId)) {
            ExpandCollapseState value=ExpandCollapseState_LeafNode; if (SUCCEEDED(p->get_CurrentExpandCollapseState(&value))) { state_key("expand_collapse"); out << static_cast<int>(value); }
        }
        if (!password) {
            if (auto p=pattern<IUIAutomationValuePattern>(e, UIA_ValuePatternId)) {
                BSTR value=nullptr; if (SUCCEEDED(p->get_CurrentValue(&value))) { state_key("value"); out << json(bstr(value)); }
                BOOL ro=FALSE; if (SUCCEEDED(p->get_CurrentIsReadOnly(&ro))) { state_key("read_only"); out << (ro?"true":"false"); }
            }
            if (auto p=pattern<IUIAutomationRangeValuePattern>(e, UIA_RangeValuePatternId)) {
                double value=0; if (SUCCEEDED(p->get_CurrentValue(&value))) { state_key("range_value"); out << value; }
            }
        }
        out << "},\"text_ranges\":" << (password ? "[]":text_ranges(e)) << ",\"children\":[";
        com_ptr<IUIAutomationElement> child; if (!password) walker->GetFirstChildElement(e,child.put()); bool comma=false;
        while (child && count<5000) {
            if (comma) out << ','; comma=true; out << node(child.get(),depth+1);
            com_ptr<IUIAutomationElement> next; walker->GetNextSiblingElement(child.get(),next.put()); child=std::move(next);
        }
        if (child) truncated=true;
        out << "]}"; return out.str();
    }
};
struct Snapshot { HANDLE done=CreateEventW(nullptr,TRUE,FALSE,nullptr); std::string root,error; bool truncated=false; ~Snapshot(){ CloseHandle(done); } };
std::shared_ptr<Snapshot> read_uia(HWND hwnd) {
    auto result=std::make_shared<Snapshot>();
    std::thread worker([hwnd,result] {
        try {
            winrt::init_apartment(winrt::apartment_type::multi_threaded);
            { Reader reader; com_ptr<IUIAutomationElement> root;
              check_hresult(reader.automation->ElementFromHandle(hwnd,root.put()));
              result->root=reader.node(root.get()); result->truncated=reader.truncated; }
            winrt::uninit_apartment();
        } catch (const winrt::hresult_error& e) { result->error=utf8(e.message().c_str()); }
          catch (const std::exception& e) { result->error=e.what(); }
        SetEvent(result->done);
    });
    if (WaitForSingleObject(result->done,20000)!=WAIT_OBJECT_0) {
        worker.detach(); throw std::runtime_error("UI Automation provider did not respond within 20 seconds");
    }
    worker.join();
    if (!result->error.empty()) throw std::runtime_error(result->error);
    return result;
}

// Picker freezes the desktop visually, so its click cannot activate a target control.
struct Picker { HBITMAP desktop=nullptr; RECT screen{}; HWND selected=nullptr; HWND overlay=nullptr; bool done=false; };
BOOL CALLBACK pick_candidate(HWND hwnd, LPARAM param) {
    auto pair=reinterpret_cast<std::pair<POINT,HWND*>*>(param);
    DWORD pid=0; GetWindowThreadProcessId(hwnd,&pid);
    if (pid==GetCurrentProcessId() || !IsWindowVisible(hwnd) || IsIconic(hwnd)) return TRUE;
    DWORD cloaked=0; DwmGetWindowAttribute(hwnd,DWMWA_CLOAKED,&cloaked,sizeof(cloaked)); if (cloaked) return TRUE;
    RECT r{}; if (FAILED(DwmGetWindowAttribute(hwnd,DWMWA_EXTENDED_FRAME_BOUNDS,&r,sizeof(r))) && !GetWindowRect(hwnd,&r)) return TRUE; if (PtInRect(&r,pair->first)) { *pair->second=hwnd; return FALSE; }
    return TRUE;
}
HWND under_pointer() { POINT pt{}; GetCursorPos(&pt); HWND found=nullptr; std::pair<POINT,HWND*> p{pt,&found}; EnumWindows(pick_candidate,reinterpret_cast<LPARAM>(&p)); return found; }
LRESULT CALLBACK picker_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    auto p=reinterpret_cast<Picker*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));
    if (msg==WM_NCCREATE) { p=reinterpret_cast<Picker*>(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams); SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(p)); }
    if (!p) return DefWindowProcW(hwnd,msg,wp,lp);
    if (msg==WM_MOUSEMOVE) { HWND next=under_pointer(); if (next!=p->selected) { p->selected=next; InvalidateRect(hwnd,nullptr,FALSE); } return 0; }
    if (msg==WM_LBUTTONDOWN || (msg==WM_KEYDOWN && wp==VK_RETURN)) { p->done=true; return 0; }
    if (msg==WM_CLOSE || (msg==WM_KEYDOWN && wp==VK_ESCAPE)) { p->selected=nullptr; p->done=true; return 0; }
    if (msg==WM_PAINT) {
        PAINTSTRUCT ps{}; HDC dc=BeginPaint(hwnd,&ps), memory=CreateCompatibleDC(dc);
        auto old=SelectObject(memory,p->desktop);
        BitBlt(dc,0,0,p->screen.right-p->screen.left,p->screen.bottom-p->screen.top,memory,0,0,SRCCOPY);
        SelectObject(memory,old); DeleteDC(memory);
        if (p->selected) {
            RECT r=bounds(p->selected); OffsetRect(&r,-p->screen.left,-p->screen.top);
            HPEN pen=CreatePen(PS_SOLID,4,RGB(255,200,0)); auto op=SelectObject(dc,pen), ob=SelectObject(dc,GetStockObject(NULL_BRUSH));
            Rectangle(dc,r.left,r.top,r.right,r.bottom); SelectObject(dc,op); SelectObject(dc,ob); DeleteObject(pen);
        }
        EndPaint(hwnd,&ps); return 0;
    }
    return DefWindowProcW(hwnd,msg,wp,lp);
}
HWND pick_window() {
    Picker p; HWND previous=GetForegroundWindow();
    p.screen={GetSystemMetrics(SM_XVIRTUALSCREEN),GetSystemMetrics(SM_YVIRTUALSCREEN),0,0};
    int w=GetSystemMetrics(SM_CXVIRTUALSCREEN),h=GetSystemMetrics(SM_CYVIRTUALSCREEN); p.screen.right=p.screen.left+w; p.screen.bottom=p.screen.top+h;
    HDC screen=GetDC(nullptr),memory=CreateCompatibleDC(screen); p.desktop=CreateCompatibleBitmap(screen,w,h);
    auto old=SelectObject(memory,p.desktop); BitBlt(memory,0,0,w,h,screen,p.screen.left,p.screen.top,SRCCOPY|CAPTUREBLT);
    SelectObject(memory,old); DeleteDC(memory); ReleaseDC(nullptr,screen);
    WNDCLASSW wc{}; wc.lpfnWndProc=picker_proc; wc.hInstance=GetModuleHandleW(nullptr); wc.lpszClassName=L"SvgshotPicker"; wc.hCursor=LoadCursorW(nullptr,IDC_CROSS); RegisterClassW(&wc);
    p.overlay=CreateWindowExW(WS_EX_TOPMOST|WS_EX_TOOLWINDOW,wc.lpszClassName,L"Pick a window: click or Enter; Esc cancels",WS_POPUP,p.screen.left,p.screen.top,w,h,nullptr,nullptr,wc.hInstance,&p);
    p.selected=under_pointer(); ShowWindow(p.overlay,SW_SHOW); SetForegroundWindow(p.overlay);
    MSG msg{};
    while (!p.done && GetMessageW(&msg,nullptr,0,0)>0) { TranslateMessage(&msg); DispatchMessageW(&msg); }
    DestroyWindow(p.overlay); DeleteObject(p.desktop); if (previous) SetForegroundWindow(previous);
    return p.selected;
}
void save_png(const std::wstring& path, UINT width, UINT height, UINT stride, BYTE* pixels) {
    com_ptr<IWICImagingFactory> factory; check_hresult(CoCreateInstance(CLSID_WICImagingFactory,nullptr,CLSCTX_INPROC_SERVER,IID_PPV_ARGS(factory.put())));
    com_ptr<IWICStream> stream; check_hresult(factory->CreateStream(stream.put())); check_hresult(stream->InitializeFromFilename(path.c_str(),GENERIC_WRITE));
    com_ptr<IWICBitmapEncoder> encoder; check_hresult(factory->CreateEncoder(GUID_ContainerFormatPng,nullptr,encoder.put())); check_hresult(encoder->Initialize(stream.get(),WICBitmapEncoderNoCache));
    com_ptr<IWICBitmapFrameEncode> frame; check_hresult(encoder->CreateNewFrame(frame.put(),nullptr)); check_hresult(frame->Initialize(nullptr)); check_hresult(frame->SetSize(width,height));
    WICPixelFormatGUID format=GUID_WICPixelFormat32bppBGRA; check_hresult(frame->SetPixelFormat(&format));
    if (format!=GUID_WICPixelFormat32bppBGRA) throw std::runtime_error("PNG encoder does not support BGRA");
    check_hresult(frame->WritePixels(height,stride,stride*height,pixels)); check_hresult(frame->Commit()); check_hresult(encoder->Commit());
}
SIZE capture_png(HWND hwnd, const std::wstring& path) {
    if (!capture::GraphicsCaptureSession::IsSupported()) throw std::runtime_error("Windows Graphics Capture is unavailable on this desktop");
    com_ptr<ID3D11Device> device; com_ptr<ID3D11DeviceContext> context;
    HRESULT hr=D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_HARDWARE,nullptr,D3D11_CREATE_DEVICE_BGRA_SUPPORT,nullptr,0,D3D11_SDK_VERSION,device.put(),nullptr,context.put());
    if (FAILED(hr)) check_hresult(D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_WARP,nullptr,D3D11_CREATE_DEVICE_BGRA_SUPPORT,nullptr,0,D3D11_SDK_VERSION,device.put(),nullptr,context.put()));
    auto dxgi=device.as<IDXGIDevice>(); com_ptr<IInspectable> inspectable;
    check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi.get(),inspectable.put()));
    auto runtimeDevice=inspectable.as<winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice>();
    auto interop=winrt::get_activation_factory<capture::GraphicsCaptureItem,IGraphicsCaptureItemInterop>();
    capture::GraphicsCaptureItem item{nullptr}; check_hresult(interop->CreateForWindow(hwnd,winrt::guid_of<capture::GraphicsCaptureItem>(),winrt::put_abi(item)));
    auto pool=capture::Direct3D11CaptureFramePool::CreateFreeThreaded(runtimeDevice,winrt::Windows::Graphics::DirectX::DirectXPixelFormat::B8G8R8A8UIntNormalized,2,item.Size());
    auto session=pool.CreateCaptureSession(item);
    try { session.IsCursorCaptureEnabled(false); } catch (const winrt::hresult_error&) { }
    struct FrameState { std::mutex mutex; std::condition_variable ready; capture::Direct3D11CaptureFrame frame{nullptr}; };
    auto state=std::make_shared<FrameState>();
    auto token=pool.FrameArrived([state](auto const& sender, auto const&) { std::lock_guard<std::mutex> lock(state->mutex); if (!state->frame) { state->frame=sender.TryGetNextFrame(); state->ready.notify_one(); } });
    session.StartCapture();
    {
        std::unique_lock<std::mutex> lock(state->mutex);
        if (!state->ready.wait_for(lock,std::chrono::seconds(8),[&]{return static_cast<bool>(state->frame);})) {
            lock.unlock();
            // Close before destroying callback state.
            pool.FrameArrived(token); session.Close(); pool.Close();
            throw std::runtime_error("No captured frame arrived within 8 seconds (window minimized or capture blocked)");
        }
    }
    pool.FrameArrived(token);
    auto frame=state->frame;
    auto access=frame.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>(); com_ptr<ID3D11Texture2D> texture;
    check_hresult(access->GetInterface(__uuidof(ID3D11Texture2D),texture.put_void()));
    auto size=frame.ContentSize(); D3D11_TEXTURE2D_DESC desc{}; texture->GetDesc(&desc);
    if (size.Width<=0 || size.Height<=0 || size.Width>static_cast<int>(desc.Width) || size.Height>static_cast<int>(desc.Height)) throw std::runtime_error("Window changed size during capture; try again");
    desc.Usage=D3D11_USAGE_STAGING; desc.BindFlags=0; desc.CPUAccessFlags=D3D11_CPU_ACCESS_READ; desc.MiscFlags=0;
    com_ptr<ID3D11Texture2D> staging; check_hresult(device->CreateTexture2D(&desc,nullptr,staging.put())); context->CopyResource(staging.get(),texture.get());
    D3D11_MAPPED_SUBRESOURCE mapped{}; check_hresult(context->Map(staging.get(),0,D3D11_MAP_READ,0,&mapped));
    try { save_png(path,size.Width,size.Height,mapped.RowPitch,static_cast<BYTE*>(mapped.pData)); }
    catch (...) { context->Unmap(staging.get(),0); throw; }
    context->Unmap(staging.get(),0); frame.Close(); session.Close(); pool.Close(); return {size.Width,size.Height};
}
LRESULT CALLBACK fixture_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg==WM_DESTROY) { PostQuitMessage(0); return 0; }
    return DefWindowProcW(hwnd,msg,wp,lp);
}
int fixture() {
    WNDCLASSW wc{}; wc.lpfnWndProc=fixture_proc; wc.hInstance=GetModuleHandleW(nullptr); wc.lpszClassName=L"SvgshotFixture"; wc.hbrBackground=reinterpret_cast<HBRUSH>(COLOR_WINDOW+1); RegisterClassW(&wc);
    HWND hwnd=CreateWindowW(wc.lpszClassName,L"svgshot Capture Fixture",WS_OVERLAPPEDWINDOW|WS_VISIBLE,50,50,450,220,nullptr,nullptr,wc.hInstance,nullptr);
    CreateWindowW(L"BUTTON",L"Add…",WS_CHILD|WS_VISIBLE|BS_PUSHBUTTON,20,30,100,30,hwnd,nullptr,wc.hInstance,nullptr);
    HWND check=CreateWindowW(L"BUTTON",L"Preserve semantics",WS_CHILD|WS_VISIBLE|BS_AUTOCHECKBOX,20,75,220,30,hwnd,nullptr,wc.hInstance,nullptr); SendMessageW(check,BM_SETCHECK,BST_CHECKED,0);
    CreateWindowW(L"EDIT",L"Exact text: <SVG> & UIA",WS_CHILD|WS_VISIBLE|WS_BORDER|ES_AUTOHSCROLL,20,120,300,30,hwnd,nullptr,wc.hInstance,nullptr);
    MSG msg{}; while (GetMessageW(&msg,nullptr,0,0)>0) { TranslateMessage(&msg); DispatchMessageW(&msg); } return 0;
}
int wmain(int argc, wchar_t** argv) {
    try {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        winrt::init_apartment(winrt::apartment_type::multi_threaded);
        HWND hwnd=nullptr; std::wstring prefix; bool uiaOnly=false, foreground=false; int delay=0;
        for (int i=1; i<argc; ++i) {
            std::wstring arg=argv[i];
            if (arg==L"--fixture") return fixture();
            if (arg==L"--out" && i+1<argc) prefix=argv[++i];
            else if (arg==L"--hwnd" && i+1<argc) hwnd=reinterpret_cast<HWND>(std::stoull(argv[++i],nullptr,0));
            else if (arg==L"--foreground") foreground=true;
            else if (arg==L"--delay" && i+1<argc) delay=std::stoi(argv[++i]);
            else if (arg==L"--uia-only") uiaOnly=true;
            else if (arg==L"--help") { std::cout << "svgshot-capture --out PREFIX [--hwnd NUMBER | --foreground --delay SECONDS] [--uia-only]\nSelect a window by clicking it; Esc cancels. Requires Windows 10 1903+.\n"; return 0; }
            else throw std::runtime_error("Unknown or incomplete option");
        }
        if (prefix.empty()) throw std::runtime_error("--out PREFIX is required");
        if (delay<0 || delay>60) throw std::runtime_error("Delay must be between 0 and 60 seconds");
        if (hwnd && foreground) throw std::runtime_error("Choose --hwnd or --foreground");
        if (delay) std::this_thread::sleep_for(std::chrono::seconds(delay));
        if (foreground) hwnd=GetForegroundWindow();
        if (!hwnd) hwnd=pick_window();
        if (!hwnd) { std::cout << "Capture cancelled\n"; return 2; }
        if (!IsWindow(hwnd) || IsIconic(hwnd)) throw std::runtime_error("Target is missing or minimized");
        RECT before=bounds(hwnd);
        auto snapshot=read_uia(hwnd);
        SIZE size{before.right-before.left,before.bottom-before.top};
        if (!uiaOnly) size=capture_png(hwnd,prefix+L".png");
        RECT after=bounds(hwnd);
        if (!EqualRect(&before,&after)) throw std::runtime_error("Window moved or resized during capture; try again");
        // WGC and DWM normally agree; some window frames instead match GetWindowRect.
        RECT conventional{}; GetWindowRect(hwnd,&conventional);
        if (size.cx==conventional.right-conventional.left && size.cy==conventional.bottom-conventional.top) before=conventional;
        bool mapped=size.cx!=before.right-before.left || size.cy!=before.bottom-before.top;
        std::ofstream out(std::filesystem::path(prefix+L".uia.json"),std::ios::binary);
        if (!out) throw std::runtime_error("Cannot write UIA snapshot");
        out << "{\"version\":1,\"screen_bounds\":" << rect_json(before) << ",\"image_size\":[" << size.cx << ',' << size.cy << "],\"warnings\":[";
        bool warning=false;
        if (snapshot->truncated) { out << "\"UIA tree truncated at 5000 elements or 64 levels\""; warning=true; }
        if (mapped) { if (warning) out << ','; out << "\"Capture size differs from window bounds; coordinates require scaling. Inspect the result.\""; }
        out << "],\"root\":" << snapshot->root << "}\n";
        if (!out) throw std::runtime_error("Failed to write UIA snapshot");
        return 0;
    } catch (const winrt::hresult_error& e) { std::cerr << "svgshot-capture: " << utf8(e.message().c_str()) << '\n'; return 1; }
      catch (const std::exception& e) { std::cerr << "svgshot-capture: " << e.what() << '\n'; return 1; }
}
