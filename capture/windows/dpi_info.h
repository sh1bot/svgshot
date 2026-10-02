// DPI observations, not an instruction to resize or redraw the target.
// Window DPI describes its coordinate context, not the source DPI of its icons.
// Keep Windows-specific facts in native.snapshot.dpi; do not apply them to the
// canonical PNG geometry. Failed queries are explicit observations, never 96.
#pragma once
#include <shellscalingapi.h>

namespace dpi_info
{
struct Context
{
    DPI_AWARENESS_CONTEXT previous;
    explicit Context(DPI_AWARENESS_CONTEXT requested)
        : previous(requested ? SetThreadDpiAwarenessContext(requested) : nullptr) {}
    ~Context() { if (previous) SetThreadDpiAwarenessContext(previous); }
    Context(const Context &) = delete;
    Context &operator=(const Context &) = delete;
};

const char *awareness(DPI_AWARENESS_CONTEXT context)
{
    if (!context) return "unknown";
    if (AreDpiAwarenessContextsEqual(context, DPI_AWARENESS_CONTEXT_UNAWARE_GDISCALED))
        return "unaware-gdi-scaled";
    if (AreDpiAwarenessContextsEqual(context, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
        return "per-monitor-v2";
    switch (GetAwarenessFromDpiAwarenessContext(context))
    {
    case DPI_AWARENESS_UNAWARE: return "unaware";
    case DPI_AWARENESS_SYSTEM_AWARE: return "system";
    case DPI_AWARENESS_PER_MONITOR_AWARE: return "per-monitor";
    default: return "unknown";
    }
}

DPI_AWARENESS_CONTEXT process_context(HWND hwnd)
{
    DWORD pid = 0;
    if (!GetWindowThreadProcessId(hwnd, &pid)) return nullptr;
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process) return nullptr;
    auto context = GetDpiAwarenessContextForProcess(process);
    DWORD failure = context ? ERROR_SUCCESS : GetLastError();
    CloseHandle(process);
    SetLastError(failure);
    return context;
}

DPI_AWARENESS_CONTEXT bitmap_context(HWND hwnd, const std::wstring &choice)
{
    if (choice == L"window") return GetWindowDpiAwarenessContext(hwnd);
    if (choice == L"application") return process_context(hwnd);
    return DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2;
}

std::string error(DWORD code, const char *domain = "win32")
{
    return "{\"status\":\"error\",\"error_domain\":\"" + std::string(domain) +
           "\",\"error_code\":" + std::to_string(code) + "}";
}
std::string value(UINT number)
{
    return number ? "{\"status\":\"value\",\"value\":" + std::to_string(number) + "}"
                  : error(ERROR_INVALID_DATA);
}
std::string rectangle(BOOL succeeded, RECT r, DWORD code)
{
    return succeeded ? "{\"status\":\"value\",\"value\":" + rect_json(r) + "}" : error(code);
}

// GetDpiForMonitor is explicitly unsuitable for a per-monitor-aware caller.
// A hidden PMv2 window owned by the collector lets GetDpiForWindow report the
// display DPI independently of the target. It is never shown or activated.
std::string display_dpi(RECT monitor)
{
    HWND probe = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, L"STATIC", L"",
        WS_POPUP, monitor.left + 1, monitor.top + 1, 1, 1, nullptr, nullptr,
        GetModuleHandleW(nullptr), nullptr);
    if (!probe) return error(GetLastError());
    UINT dpi = GetDpiForWindow(probe);
    DestroyWindow(probe);
    return value(dpi);
}

struct Monitors
{
    std::vector<HMONITOR> handles;
    bool failed = false;
};
BOOL CALLBACK collect_monitor(HMONITOR monitor, HDC, LPRECT, LPARAM parameter)
{
    auto &list = *reinterpret_cast<Monitors *>(parameter);
    try { list.handles.push_back(monitor); }
    catch (...) { list.failed = true; return FALSE; }
    return TRUE;
}

std::string read(HWND hwnd)
{
    auto collector = GetThreadDpiAwarenessContext();
    auto target = GetWindowDpiAwarenessContext(hwnd);
    Context physical(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    // Never silently label virtualized data as physical pixels.
    if (!physical.previous) return error(GetLastError());
    UINT systemDpi = GetDpiForSystem(), windowDpi = GetDpiForWindow(hwnd);
    RECT window{}, client{}, logical{};
    BOOL haveWindow = GetWindowRect(hwnd, &window);
    DWORD windowError = haveWindow ? ERROR_SUCCESS : GetLastError();
    BOOL haveLogical = FALSE, haveClient = FALSE;
    DWORD logicalError = ERROR_INVALID_HANDLE, clientError = ERROR_INVALID_HANDLE;
    {
        Context native(target);
        if (native.previous)
        {
            haveLogical = GetWindowRect(hwnd, &logical);
            logicalError = haveLogical ? ERROR_SUCCESS : GetLastError();
            haveClient = GetClientRect(hwnd, &client);
            clientError = haveClient ? ERROR_SUCCESS : GetLastError();
        }
    }
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    PROCESS_DPI_AWARENESS processMode = PROCESS_DPI_UNAWARE;
    HRESULT processResult = process ? GetProcessDpiAwareness(process, &processMode)
                                    : HRESULT_FROM_WIN32(GetLastError());
    if (process) CloseHandle(process);
    std::string processObservation = FAILED(processResult)
        ? error(static_cast<DWORD>(processResult), "hresult")
        : std::string("{\"status\":\"value\",\"value\":\"") +
          (processMode == PROCESS_DPI_UNAWARE ? "unaware" :
           processMode == PROCESS_SYSTEM_DPI_AWARE ? "system" : "per-monitor") + "\"}";
    auto processContext = process_context(hwnd);
    std::string processContextObservation = processContext
        ? std::string("{\"status\":\"value\",\"value\":\"") + awareness(processContext) + "\"}"
        : error(GetLastError());

    Monitors monitors;
    HMONITOR nearest = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
    SetLastError(ERROR_SUCCESS);
    BOOL enumerated = haveWindow && EnumDisplayMonitors(nullptr, &window, collect_monitor,
                                                       reinterpret_cast<LPARAM>(&monitors));
    DWORD enumerationError = GetLastError();
    // Offscreen windows still have a DPI associated with their nearest display.
    if (monitors.handles.empty() && nearest) monitors.handles.push_back(nearest);
    std::ostringstream out;
    out << "{\"collector_awareness\":\"" << awareness(collector)
        << "\",\"measurement_awareness\":\"per-monitor-v2\",\"system_dpi\":" << value(systemDpi)
        << ",\"window\":{\"dpi\":" << value(windowDpi)
        << ",\"awareness\":\"" << awareness(target)
        << "\",\"process_awareness\":" << processObservation
        << ",\"process_context\":" << processContextObservation
        << ",\"physical_window_bounds\":" << rectangle(haveWindow, window, windowError)
        << ",\"context_window_bounds\":" << rectangle(haveLogical, logical, logicalError)
        << ",\"context_client_bounds\":" << rectangle(haveClient, client, clientError)
        << "},\"display_enumeration\":"
        << (enumerated && !monitors.failed ? "{\"status\":\"value\",\"value\":true}"
                                          : error(monitors.failed ? ERROR_NOT_ENOUGH_MEMORY : enumerationError))
        << ",\"displays\":[";
    bool first = true;
    for (HMONITOR monitor : monitors.handles)
    {
        if (!first) out << ',';
        first = false;
        MONITORINFO info{};
        info.cbSize = sizeof(info);
        if (!GetMonitorInfoW(monitor, &info)) { out << error(GetLastError()); continue; }
        RECT overlap{};
        bool intersects = haveWindow && IntersectRect(&overlap, &window, &info.rcMonitor);
        DEVICE_SCALE_FACTOR scale = SCALE_100_PERCENT;
        HRESULT scaleResult = GetScaleFactorForMonitor(monitor, &scale);
        out << "{\"status\":\"value\",\"bounds\":" << rect_json(info.rcMonitor)
            << ",\"primary\":" << ((info.dwFlags & MONITORINFOF_PRIMARY) ? "true" : "false")
            << ",\"window_monitor\":" << (monitor == nearest ? "true" : "false")
            << ",\"intersects_window\":" << (intersects ? "true" : "false")
            << ",\"effective_dpi\":" << display_dpi(info.rcMonitor)
            << ",\"scale_percent\":" << (SUCCEEDED(scaleResult) ? value(static_cast<UINT>(scale))
                                                                  : error(static_cast<DWORD>(scaleResult), "hresult")) << '}';
    }
    out << "]}";
    return out.str();
}
} // namespace dpi_info
