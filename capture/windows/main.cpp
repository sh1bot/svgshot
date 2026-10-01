// UI Automation + Windows Graphics Capture + WIC PNG, with vendored miniz.
#include <windows.h>
#include <ole2.h>
#include <UIAutomation.h>
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <d3d11.h>
#include <dwmapi.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#include <wincodec.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <winrt/Windows.Graphics.DirectX.h>
using winrt::check_hresult;
using winrt::com_ptr;
namespace capture = winrt::Windows::Graphics::Capture;

std::string utf8(const std::wstring &s)
{
    if (s.empty())
        return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0,
                                nullptr, nullptr);
    std::string out(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), out.data(), n, nullptr,
                        nullptr);
    return out;
}
std::string json(const std::wstring &s)
{
    std::string out = "\"";
    for (unsigned char c : utf8(s))
    {
        if (c == '"' || c == '\\')
        {
            out += '\\';
            out += c;
        }
        else if (c < 32)
        {
            char b[7];
            sprintf_s(b, "\\u%04x", c);
            out += b;
        }
        else
            out += c;
    }
    return out + '"';
}
std::wstring bstr(BSTR value)
{
    std::wstring out = value ? std::wstring(value, SysStringLen(value)) : L"";
    SysFreeString(value);
    return out;
}
std::string rect_json(RECT r)
{
    return "[" + std::to_string(r.left) + "," + std::to_string(r.top) + "," +
           std::to_string(r.right - r.left) + "," + std::to_string(r.bottom - r.top) + "]";
}
RECT bounds(HWND hwnd)
{
    RECT r{};
    if (FAILED(DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, &r, sizeof(r))))
        if (!GetWindowRect(hwnd, &r))
            throw std::runtime_error("Cannot read window bounds");
    return r;
}
std::string runtime_id(IUIAutomationElement *e)
{
    SAFEARRAY *a = nullptr;
    if (FAILED(e->GetRuntimeId(&a)) || !a)
        return {};
    LONG low = 0, high = -1;
    SafeArrayGetLBound(a, 1, &low);
    SafeArrayGetUBound(a, 1, &high);
    std::string result = "uia";
    for (LONG i = low; i <= high; ++i)
    {
        int value = 0;
        SafeArrayGetElement(a, &i, &value);
        result += "-" + std::to_string(value);
    }
    SafeArrayDestroy(a);
    return result;
}
template <typename T> com_ptr<T> pattern(IUIAutomationElement *e, PATTERNID id)
{
    com_ptr<T> p;
    e->GetCurrentPatternAs(id, __uuidof(T), p.put_void());
    return p;
}
#include "uia_data.h"
#include "png_snapshot.h"

std::string range_details(IUIAutomationTextRange *range, UiaData &data)
{
    std::ostringstream out;
    BSTR raw = nullptr;
    HRESULT hr = range->GetText(65537, &raw);
    auto text = bstr(raw);
    bool truncated = text.size() > 65536;
    if (truncated)
    {
        text.resize(65536);
        data.text_truncated = true;
    }
    out << "{\"text\":" << json(text)
        << ",\"text_status\":" << (FAILED(hr) ? UiaData::failure(hr) : "{\"status\":\"value\"}")
        << ",\"truncated\":" << (truncated ? "true" : "false") << ",\"rectangles\":[";
    SAFEARRAY *rectangles = nullptr;
    hr = range->GetBoundingRectangles(&rectangles);
    if (SUCCEEDED(hr) && rectangles)
    {
        LONG lo = 0, hi = -1;
        SafeArrayGetLBound(rectangles, 1, &lo);
        SafeArrayGetUBound(rectangles, 1, &hi);
        for (LONG i = lo; i + 3 <= hi; i += 4)
        {
            if (i != lo)
                out << ',';
            out << '[';
            for (LONG j = i; j < i + 4; ++j)
            {
                double value = 0;
                SafeArrayGetElement(rectangles, &j, &value);
                if (j != i)
                    out << ',';
                out << (std::isfinite(value) ? value : 0);
            }
            out << ']';
        }
    }
    if (rectangles)
        SafeArrayDestroy(rectangles);
    out << "],\"bounds_status\":" << (FAILED(hr) ? UiaData::failure(hr) : "{\"status\":\"value\"}")
        << ",\"attributes\":" << data.attributes(range);
    com_ptr<IUIAutomationElement> enclosing;
    hr = range->GetEnclosingElement(enclosing.put());
    out << ",\"enclosing_element\":\"" << (enclosing ? runtime_id(enclosing.get()) : "") << "\"";
    if (FAILED(hr))
        out << ",\"enclosing_error\":" << UiaData::failure(hr);
    com_ptr<IUIAutomationElementArray> children;
    hr = range->GetChildren(children.put());
    out << ",\"children\":[";
    int count = 0;
    if (children)
        children->get_Length(&count);
    for (int i = 0; i < count && i < 5000; ++i)
    {
        com_ptr<IUIAutomationElement> child;
        children->GetElement(i, child.put());
        if (i)
            out << ',';
        out << '\"' << (child ? runtime_id(child.get()) : "") << '\"';
    }
    out << ']';
    if (count > 5000)
    {
        out << ",\"children_truncated\":true";
        data.text_truncated = true;
    }
    if (FAILED(hr))
        out << ",\"children_error\":" << UiaData::failure(hr);
    return out.str() + '}';
}

std::string format_runs(IUIAutomationTextRange *range, UiaData &data, int &budget)
{
    com_ptr<IUIAutomationTextRange> run;
    HRESULT hr = range->Clone(run.put());
    if (FAILED(hr))
        return UiaData::failure(hr);
    run->MoveEndpointByRange(TextPatternRangeEndpoint_End, run.get(),
                             TextPatternRangeEndpoint_Start);
    std::string out = "{\"status\":\"value\",\"ranges\":[";
    bool comma = false, truncated = false;
    while (true)
    {
        int cmp = 0;
        hr = run->CompareEndpoints(TextPatternRangeEndpoint_Start, range,
                                   TextPatternRangeEndpoint_End, &cmp);
        if (FAILED(hr) || cmp >= 0)
            break;
        if (budget-- <= 0)
        {
            truncated = true;
            break;
        }
        int moved = 0;
        hr = run->MoveEndpointByUnit(TextPatternRangeEndpoint_End, TextUnit_Format, 1, &moved);
        if (FAILED(hr) || !moved)
            break;
        run->CompareEndpoints(TextPatternRangeEndpoint_End, range, TextPatternRangeEndpoint_End,
                              &cmp);
        if (cmp > 0)
            run->MoveEndpointByRange(TextPatternRangeEndpoint_End, range,
                                     TextPatternRangeEndpoint_End);
        run->CompareEndpoints(TextPatternRangeEndpoint_Start, run.get(),
                              TextPatternRangeEndpoint_End, &cmp);
        if (cmp >= 0)
        {
            truncated = true;
            break;
        }
        if (comma)
            out += ',';
        comma = true;
        out += range_details(run.get(), data);
        run->MoveEndpointByRange(TextPatternRangeEndpoint_Start, run.get(),
                                 TextPatternRangeEndpoint_End);
    }
    out += ']';
    if (FAILED(hr))
        out += ",\"error\":" + UiaData::failure(hr);
    if (truncated)
    {
        data.text_truncated = true;
        out += ",\"truncated\":true";
    }
    return out + '}';
}

std::string text_selection(IUIAutomationElement *e, UiaData &data, bool include_hidden)
{
    auto p = pattern<IUIAutomationTextPattern>(e, UIA_TextPatternId);
    if (!p)
        return "{\"status\":\"not_supported\"}";
    com_ptr<IUIAutomationTextRangeArray> ranges;
    HRESULT hr = p->GetSelection(ranges.put());
    if (FAILED(hr))
        return UiaData::failure(hr);
    int count = 0;
    if (ranges)
        ranges->get_Length(&count);
    SupportedTextSelection supported = SupportedTextSelection_None;
    HRESULT selection_hr = p->get_SupportedTextSelection(&supported);
    std::string out = "{\"status\":\"value\",\"supported_selection\":" + std::to_string(supported);
    if (FAILED(selection_hr))
        out += ",\"supported_selection_error\":" + UiaData::failure(selection_hr);
    out += ",\"ranges\":[";
    com_ptr<IUIAutomationTextRangeArray> visible;
    if (!include_hidden)
    {
        hr = p->GetVisibleRanges(visible.put());
        if (FAILED(hr))
            return UiaData::failure(hr);
    }
    int visible_count = 0, emitted = 0;
    if (visible)
        visible->get_Length(&visible_count);
    bool comma = false;
    for (int i = 0; i < count && emitted < 256; ++i)
    {
        com_ptr<IUIAutomationTextRange> selected;
        hr = ranges->GetElement(i, selected.put());
        if (FAILED(hr) || !selected)
            continue;
        for (int j = 0; j < (include_hidden ? 1 : std::min(visible_count, 2000)) && emitted < 256;
             ++j)
        {
            com_ptr<IUIAutomationTextRange> range;
            selected->Clone(range.put());
            if (!range)
                continue;
            if (!include_hidden)
            {
                com_ptr<IUIAutomationTextRange> viewport;
                visible->GetElement(j, viewport.put());
                if (!viewport)
                    continue;
                int cmp = 0;
                if (FAILED(range->CompareEndpoints(TextPatternRangeEndpoint_Start, viewport.get(),
                                                   TextPatternRangeEndpoint_Start, &cmp)))
                    continue;
                if (cmp < 0)
                    range->MoveEndpointByRange(TextPatternRangeEndpoint_Start, viewport.get(),
                                               TextPatternRangeEndpoint_Start);
                if (FAILED(range->CompareEndpoints(TextPatternRangeEndpoint_End, viewport.get(),
                                                   TextPatternRangeEndpoint_End, &cmp)))
                    continue;
                if (cmp > 0)
                    range->MoveEndpointByRange(TextPatternRangeEndpoint_End, viewport.get(),
                                               TextPatternRangeEndpoint_End);
                range->CompareEndpoints(TextPatternRangeEndpoint_Start, range.get(),
                                        TextPatternRangeEndpoint_End, &cmp);
                if (cmp >= 0)
                    continue;
            }
            if (comma)
                out += ',';
            comma = true;
            out += range_details(range.get(), data);
            ++emitted;
        }
    }
    bool truncated = emitted >= 256 || (!include_hidden && visible_count > 2000);
    if (truncated)
        data.text_truncated = true;
    return out + "],\"visible_only\":" + (include_hidden ? "false" : "true") +
           ",\"truncated\":" + (truncated ? "true}" : "false}");
}

std::string text_ranges(IUIAutomationElement *e, UiaData &data, std::string &status)
{
    auto p = pattern<IUIAutomationTextPattern>(e, UIA_TextPatternId);
    if (!p)
    {
        status = "{\"status\":\"not_supported\"}";
        return "[]";
    }
    com_ptr<IUIAutomationTextRangeArray> ranges;
    HRESULT visible_hr = p->GetVisibleRanges(ranges.put());
    if (FAILED(visible_hr) || !ranges)
    {
        status = UiaData::failure(FAILED(visible_hr) ? visible_hr : E_FAIL);
        return "[]";
    }
    status = "{\"status\":\"value\"}";
    int n = 0;
    ranges->get_Length(&n);
    std::ostringstream out;
    out << '[';
    bool comma = false;
    int budget = 2000, format_budget = 2048;
    for (int i = 0; i < n && budget > 0; ++i)
    {
        com_ptr<IUIAutomationTextRange> visible, line;
        if (FAILED(ranges->GetElement(i, visible.put())) || !visible)
            continue;
        if (FAILED(visible->Clone(line.put())) || !line)
            continue;
        line->MoveEndpointByRange(TextPatternRangeEndpoint_End, visible.get(),
                                  TextPatternRangeEndpoint_Start);
        line->ExpandToEnclosingUnit(TextUnit_Line);
        while (budget-- > 0)
        {
            int cmp = 0;
            line->CompareEndpoints(TextPatternRangeEndpoint_Start, visible.get(),
                                   TextPatternRangeEndpoint_End, &cmp);
            if (cmp >= 0)
                break;
            com_ptr<IUIAutomationTextRange> clipped;
            if (FAILED(line->Clone(clipped.put())))
                break;
            clipped->CompareEndpoints(TextPatternRangeEndpoint_Start, visible.get(),
                                      TextPatternRangeEndpoint_Start, &cmp);
            if (cmp < 0)
                clipped->MoveEndpointByRange(TextPatternRangeEndpoint_Start, visible.get(),
                                             TextPatternRangeEndpoint_Start);
            clipped->CompareEndpoints(TextPatternRangeEndpoint_End, visible.get(),
                                      TextPatternRangeEndpoint_End, &cmp);
            if (cmp > 0)
                clipped->MoveEndpointByRange(TextPatternRangeEndpoint_End, visible.get(),
                                             TextPatternRangeEndpoint_End);
            BSTR raw = nullptr;
            HRESULT text_hr = clipped->GetText(65537, &raw);
            if (FAILED(text_hr))
                status = UiaData::failure(text_hr);
            auto text = bstr(raw);
            if (text.size() > 65536)
            {
                text.resize(65536);
                data.text_truncated = true;
            }
            while (!text.empty() && (text.back() == L'\r' || text.back() == L'\n'))
                text.pop_back();
            SAFEARRAY *a = nullptr;
            HRESULT bounds_hr = clipped->GetBoundingRectangles(&a);
            if (FAILED(bounds_hr))
                status = UiaData::failure(bounds_hr);
            if (!text.empty() && SUCCEEDED(bounds_hr) && a)
            {
                LONG low = 0, high = -1;
                SafeArrayGetLBound(a, 1, &low);
                SafeArrayGetUBound(a, 1, &high);
                if (high - low + 1 >= 4)
                {
                    if (comma)
                        out << ',';
                    comma = true;
                    out << "{\"text\":" << json(text) << ",\"rectangles\":[";
                    for (LONG j = low; j + 3 <= high; j += 4)
                    {
                        if (j > low)
                            out << ',';
                        out << '[';
                        for (LONG k = j; k < j + 4; ++k)
                        {
                            double v = 0;
                            SafeArrayGetElement(a, &k, &v);
                            if (k > j)
                                out << ',';
                            out << v;
                        }
                        out << ']';
                    }
                    out << "],\"attributes\":" << data.attributes(clipped.get())
                        << ",\"format_runs\":" << format_runs(clipped.get(), data, format_budget)
                        << '}';
                }
            }
            if (a)
                SafeArrayDestroy(a);
            int moved = 0;
            if (FAILED(line->Move(TextUnit_Line, 1, &moved)) || moved == 0)
                break;
        }
    }
    if (budget <= 0)
    {
        data.text_truncated = true;
        status = "{\"status\":\"value\",\"truncated\":true}";
    }
    out << ']';
    return out.str();
}
struct Reader
{
    com_ptr<IUIAutomation> automation;
    com_ptr<IUIAutomationTreeWalker> walker;
    int count = 0;
    bool truncated = false;
    std::unique_ptr<UiaData> data;
    RECT viewport{};
    bool include_hidden = false;
    Reader(RECT area, bool allow_hidden) : viewport(area), include_hidden(allow_hidden)
    {
        check_hresult(CoCreateInstance(__uuidof(CUIAutomation), nullptr, CLSCTX_INPROC_SERVER,
                                       IID_PPV_ARGS(automation.put())));
        check_hresult(automation->get_RawViewWalker(walker.put()));
        data = std::make_unique<UiaData>(automation.get());
    }
    std::string node(IUIAutomationElement *e, int depth = 0)
    {
        if (++count > 5000 || depth > 64)
        {
            truncated = true;
            return "null";
        }
        RECT r{};
        HRESULT bounds_hr = e->get_CurrentBoundingRectangle(&r);
        BOOL offscreen = FALSE;
        HRESULT offscreen_hr = e->get_CurrentIsOffscreen(&offscreen);
        RECT intersection{};
        bool hidden = FAILED(bounds_hr) || FAILED(offscreen_hr) || offscreen ||
                      !IntersectRect(&intersection, &r, &viewport);
        bool redact_content = hidden && !include_hidden;
        CONTROLTYPEID type = 0;
        e->get_CurrentControlType(&type);
        BOOL password = FALSE;
        // Fail closed when the provider cannot establish password status.
        if (FAILED(e->get_CurrentIsPassword(&password)))
            password = TRUE;
        std::ostringstream out;
        out << "{\"id\":\"" << runtime_id(e) << "\",\"control_type\":" << type
            << ",\"bounds\":" << rect_json(r);
        auto str = [&](const char *key, auto getter) {
            BSTR value = nullptr;
            if (!redact_content && (include_hidden || std::string(key) != "automation_id"))
                (e->*getter)(&value);
            out << ",\"" << key << "\":" << json(bstr(value));
        };
        str("name", &IUIAutomationElement::get_CurrentName);
        str("localized_control_type", &IUIAutomationElement::get_CurrentLocalizedControlType);
        str("automation_id", &IUIAutomationElement::get_CurrentAutomationId);
        str("class_name", &IUIAutomationElement::get_CurrentClassName);
        str("framework_id", &IUIAutomationElement::get_CurrentFrameworkId);
        str("help_text", &IUIAutomationElement::get_CurrentHelpText);
        str("access_key", &IUIAutomationElement::get_CurrentAccessKey);
        str("accelerator_key", &IUIAutomationElement::get_CurrentAcceleratorKey);
        auto flag = [&](const char *key, auto getter) {
            BOOL value = FALSE;
            (e->*getter)(&value);
            out << ",\"" << key << "\":" << (value ? "true" : "false");
        };
        flag("enabled", &IUIAutomationElement::get_CurrentIsEnabled);
        flag("offscreen", &IUIAutomationElement::get_CurrentIsOffscreen);
        flag("keyboard_focus", &IUIAutomationElement::get_CurrentHasKeyboardFocus);
        flag("focusable", &IUIAutomationElement::get_CurrentIsKeyboardFocusable);
        flag("control_element", &IUIAutomationElement::get_CurrentIsControlElement);
        flag("content_element", &IUIAutomationElement::get_CurrentIsContentElement);
        out << ",\"password\":" << (password ? "true" : "false")
            << ",\"content_redacted\":" << (redact_content ? "true" : "false");
        com_ptr<IUIAutomationElement> label;
        e->get_CurrentLabeledBy(label.put());
        if (label)
            out << ",\"labeled_by\":\"" << runtime_id(label.get()) << '"';
        out << ",\"states\":{";
        bool state = false;
        auto state_key = [&](const char *key) {
            if (state)
                out << ',';
            state = true;
            out << '"' << key << "\":";
        };
        if (auto p = pattern<IUIAutomationTogglePattern>(e, UIA_TogglePatternId))
        {
            ToggleState value = ToggleState_Off;
            if (SUCCEEDED(p->get_CurrentToggleState(&value)))
            {
                state_key("toggle");
                out << static_cast<int>(value);
            }
        }
        if (auto p = pattern<IUIAutomationSelectionItemPattern>(e, UIA_SelectionItemPatternId))
        {
            BOOL value = FALSE;
            if (SUCCEEDED(p->get_CurrentIsSelected(&value)))
            {
                state_key("selected");
                out << (value ? "true" : "false");
            }
        }
        if (auto p = pattern<IUIAutomationExpandCollapsePattern>(e, UIA_ExpandCollapsePatternId))
        {
            ExpandCollapseState value = ExpandCollapseState_LeafNode;
            if (SUCCEEDED(p->get_CurrentExpandCollapseState(&value)))
            {
                state_key("expand_collapse");
                out << static_cast<int>(value);
            }
        }
        if (!password && !redact_content)
        {
            if (auto p = pattern<IUIAutomationValuePattern>(e, UIA_ValuePatternId))
            {
                if (include_hidden)
                {
                    BSTR value = nullptr;
                    if (SUCCEEDED(p->get_CurrentValue(&value)))
                    {
                        state_key("value");
                        out << json(bstr(value));
                    }
                }
                BOOL ro = FALSE;
                if (SUCCEEDED(p->get_CurrentIsReadOnly(&ro)))
                {
                    state_key("read_only");
                    out << (ro ? "true" : "false");
                }
            }
            if (auto p = pattern<IUIAutomationRangeValuePattern>(e, UIA_RangeValuePatternId))
            {
                double value = 0;
                if (SUCCEEDED(p->get_CurrentValue(&value)))
                {
                    state_key("range_value");
                    out << value;
                }
            }
        }
        std::string text_status = "{\"status\":\"redacted\"}";
        auto lines = password || redact_content ? "[]" : text_ranges(e, *data, text_status);
        out << "},\"properties\":" << data->read_properties(e, password, hidden, include_hidden)
            << ",\"patterns\":" << data->read_patterns(e) << ",\"text_ranges\":" << lines
            << ",\"text_capture\":" << text_status << ",\"text_selection\":"
            << (password || redact_content ? "{\"status\":\"redacted\"}"
                                           : text_selection(e, *data, include_hidden))
            << ",\"children\":[";
        com_ptr<IUIAutomationElement> child;
        HRESULT children_hr = S_OK;
        if (!password)
            children_hr = walker->GetFirstChildElement(e, child.put());
        bool comma = false;
        while (child && count < 5000)
        {
            if (comma)
                out << ',';
            comma = true;
            out << node(child.get(), depth + 1);
            com_ptr<IUIAutomationElement> next;
            children_hr = walker->GetNextSiblingElement(child.get(), next.put());
            child = std::move(next);
        }
        if (child)
            truncated = true;
        out << "],\"children_status\":"
            << (password              ? "{\"status\":\"redacted\"}"
                : FAILED(children_hr) ? UiaData::failure(children_hr)
                                      : "{\"status\":\"value\"}")
            << '}';
        return out.str();
    }
};
struct Snapshot
{
    HANDLE done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    std::string root, error, property_names, pattern_names;
    bool text_truncated = false;
    bool truncated = false;
    ~Snapshot()
    {
        CloseHandle(done);
    }
};
std::shared_ptr<Snapshot> read_uia(HWND hwnd, bool include_hidden)
{
    auto result = std::make_shared<Snapshot>();
    std::thread worker([hwnd, result, include_hidden] {
        try
        {
            winrt::init_apartment(winrt::apartment_type::multi_threaded);
            {
                Reader reader(bounds(hwnd), include_hidden);
                com_ptr<IUIAutomationElement> root;
                check_hresult(reader.automation->ElementFromHandle(hwnd, root.put()));
                result->root = reader.node(root.get());
                result->property_names = reader.data->property_names;
                result->pattern_names = reader.data->pattern_names;
                result->text_truncated = reader.data->text_truncated;
                result->truncated = reader.truncated;
            }
            winrt::uninit_apartment();
        }
        catch (const winrt::hresult_error &e)
        {
            result->error = utf8(e.message().c_str());
        }
        catch (const std::exception &e)
        {
            result->error = e.what();
        }
        SetEvent(result->done);
    });
    if (WaitForSingleObject(result->done, 20000) != WAIT_OBJECT_0)
    {
        worker.detach();
        throw std::runtime_error("UI Automation provider did not respond within 20 seconds");
    }
    worker.join();
    if (!result->error.empty())
        throw std::runtime_error(result->error);
    return result;
}

// Picker freezes the desktop visually, so its click cannot activate a target control.
struct Picker
{
    HBITMAP desktop = nullptr;
    RECT screen{};
    HWND selected = nullptr;
    HWND overlay = nullptr;
    bool done = false;
};
BOOL CALLBACK pick_candidate(HWND hwnd, LPARAM param)
{
    auto pair = reinterpret_cast<std::pair<POINT, HWND *> *>(param);
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    if (pid == GetCurrentProcessId() || !IsWindowVisible(hwnd) || IsIconic(hwnd))
        return TRUE;
    DWORD cloaked = 0;
    DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, &cloaked, sizeof(cloaked));
    if (cloaked)
        return TRUE;
    RECT r{};
    if (FAILED(DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, &r, sizeof(r))) &&
        !GetWindowRect(hwnd, &r))
        return TRUE;
    if (PtInRect(&r, pair->first))
    {
        *pair->second = hwnd;
        return FALSE;
    }
    return TRUE;
}
HWND under_pointer()
{
    POINT pt{};
    GetCursorPos(&pt);
    HWND found = nullptr;
    std::pair<POINT, HWND *> p{pt, &found};
    EnumWindows(pick_candidate, reinterpret_cast<LPARAM>(&p));
    return found;
}
LRESULT CALLBACK picker_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    auto p = reinterpret_cast<Picker *>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (msg == WM_NCCREATE)
    {
        p = reinterpret_cast<Picker *>(reinterpret_cast<CREATESTRUCTW *>(lp)->lpCreateParams);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(p));
    }
    if (!p)
        return DefWindowProcW(hwnd, msg, wp, lp);
    if (msg == WM_MOUSEMOVE)
    {
        HWND next = under_pointer();
        if (next != p->selected)
        {
            p->selected = next;
            InvalidateRect(hwnd, nullptr, FALSE);
        }
        return 0;
    }
    if (msg == WM_LBUTTONDOWN || (msg == WM_KEYDOWN && wp == VK_RETURN))
    {
        p->done = true;
        return 0;
    }
    if (msg == WM_CLOSE || (msg == WM_KEYDOWN && wp == VK_ESCAPE))
    {
        p->selected = nullptr;
        p->done = true;
        return 0;
    }
    if (msg == WM_PAINT)
    {
        PAINTSTRUCT ps{};
        HDC dc = BeginPaint(hwnd, &ps), memory = CreateCompatibleDC(dc);
        auto old = SelectObject(memory, p->desktop);
        BitBlt(dc, 0, 0, p->screen.right - p->screen.left, p->screen.bottom - p->screen.top, memory,
               0, 0, SRCCOPY);
        SelectObject(memory, old);
        DeleteDC(memory);
        if (p->selected)
        {
            RECT r = bounds(p->selected);
            OffsetRect(&r, -p->screen.left, -p->screen.top);
            HPEN pen = CreatePen(PS_SOLID, 4, RGB(255, 200, 0));
            auto op = SelectObject(dc, pen), ob = SelectObject(dc, GetStockObject(NULL_BRUSH));
            Rectangle(dc, r.left, r.top, r.right, r.bottom);
            SelectObject(dc, op);
            SelectObject(dc, ob);
            DeleteObject(pen);
        }
        EndPaint(hwnd, &ps);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}
HWND pick_window()
{
    Picker p;
    HWND previous = GetForegroundWindow();
    p.screen = {GetSystemMetrics(SM_XVIRTUALSCREEN), GetSystemMetrics(SM_YVIRTUALSCREEN), 0, 0};
    int w = GetSystemMetrics(SM_CXVIRTUALSCREEN), h = GetSystemMetrics(SM_CYVIRTUALSCREEN);
    p.screen.right = p.screen.left + w;
    p.screen.bottom = p.screen.top + h;
    HDC screen = GetDC(nullptr), memory = CreateCompatibleDC(screen);
    p.desktop = CreateCompatibleBitmap(screen, w, h);
    auto old = SelectObject(memory, p.desktop);
    BitBlt(memory, 0, 0, w, h, screen, p.screen.left, p.screen.top, SRCCOPY | CAPTUREBLT);
    SelectObject(memory, old);
    DeleteDC(memory);
    ReleaseDC(nullptr, screen);
    WNDCLASSW wc{};
    wc.lpfnWndProc = picker_proc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"SvgshotPicker";
    wc.hCursor = LoadCursorW(nullptr, IDC_CROSS);
    RegisterClassW(&wc);
    p.overlay =
        CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, wc.lpszClassName,
                        L"Pick a window: click or Enter; Esc cancels", WS_POPUP, p.screen.left,
                        p.screen.top, w, h, nullptr, nullptr, wc.hInstance, &p);
    p.selected = under_pointer();
    ShowWindow(p.overlay, SW_SHOW);
    SetForegroundWindow(p.overlay);
    MSG msg{};
    while (!p.done && GetMessageW(&msg, nullptr, 0, 0) > 0)
    {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    DestroyWindow(p.overlay);
    DeleteObject(p.desktop);
    if (previous)
        SetForegroundWindow(previous);
    return p.selected;
}
void save_png(const std::wstring &path, UINT width, UINT height, UINT stride, BYTE *pixels)
{
    com_ptr<IWICImagingFactory> factory;
    check_hresult(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
                                   IID_PPV_ARGS(factory.put())));
    com_ptr<IWICStream> stream;
    check_hresult(factory->CreateStream(stream.put()));
    check_hresult(stream->InitializeFromFilename(path.c_str(), GENERIC_WRITE));
    com_ptr<IWICBitmapEncoder> encoder;
    check_hresult(factory->CreateEncoder(GUID_ContainerFormatPng, nullptr, encoder.put()));
    check_hresult(encoder->Initialize(stream.get(), WICBitmapEncoderNoCache));
    com_ptr<IWICBitmapFrameEncode> frame;
    check_hresult(encoder->CreateNewFrame(frame.put(), nullptr));
    check_hresult(frame->Initialize(nullptr));
    check_hresult(frame->SetSize(width, height));
    WICPixelFormatGUID format = GUID_WICPixelFormat32bppBGRA;
    check_hresult(frame->SetPixelFormat(&format));
    if (format != GUID_WICPixelFormat32bppBGRA)
        throw std::runtime_error("PNG encoder does not support BGRA");
    check_hresult(frame->WritePixels(height, stride, stride * height, pixels));
    check_hresult(frame->Commit());
    check_hresult(encoder->Commit());
}
SIZE capture_png(HWND hwnd, const std::wstring &path)
{
    if (!capture::GraphicsCaptureSession::IsSupported())
        throw std::runtime_error("Windows Graphics Capture is unavailable on this desktop");
    com_ptr<ID3D11Device> device;
    com_ptr<ID3D11DeviceContext> context;
    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
                                   D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION,
                                   device.put(), nullptr, context.put());
    if (FAILED(hr))
        check_hresult(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr,
                                        D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0,
                                        D3D11_SDK_VERSION, device.put(), nullptr, context.put()));
    auto dxgi = device.as<IDXGIDevice>();
    com_ptr<IInspectable> inspectable;
    check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi.get(), inspectable.put()));
    auto runtimeDevice =
        inspectable.as<winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice>();
    auto interop =
        winrt::get_activation_factory<capture::GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
    capture::GraphicsCaptureItem item{nullptr};
    check_hresult(interop->CreateForWindow(hwnd, winrt::guid_of<capture::GraphicsCaptureItem>(),
                                           winrt::put_abi(item)));
    auto pool = capture::Direct3D11CaptureFramePool::CreateFreeThreaded(
        runtimeDevice,
        winrt::Windows::Graphics::DirectX::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2,
        item.Size());
    auto session = pool.CreateCaptureSession(item);
    try
    {
        session.IsCursorCaptureEnabled(false);
    }
    catch (const winrt::hresult_error &)
    {
    }
    struct FrameState
    {
        std::mutex mutex;
        std::condition_variable ready;
        capture::Direct3D11CaptureFrame frame{nullptr};
    };
    auto state = std::make_shared<FrameState>();
    auto token = pool.FrameArrived([state](auto const &sender, auto const &) {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (!state->frame)
        {
            state->frame = sender.TryGetNextFrame();
            state->ready.notify_one();
        }
    });
    session.StartCapture();
    {
        std::unique_lock<std::mutex> lock(state->mutex);
        if (!state->ready.wait_for(lock, std::chrono::seconds(8),
                                   [&] { return static_cast<bool>(state->frame); }))
        {
            lock.unlock();
            // Close before destroying callback state.
            pool.FrameArrived(token);
            session.Close();
            pool.Close();
            throw std::runtime_error(
                "No captured frame arrived within 8 seconds (window minimized or capture blocked)");
        }
    }
    pool.FrameArrived(token);
    auto frame = state->frame;
    auto access = frame.Surface()
                      .as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
    com_ptr<ID3D11Texture2D> texture;
    check_hresult(access->GetInterface(__uuidof(ID3D11Texture2D), texture.put_void()));
    auto size = frame.ContentSize();
    D3D11_TEXTURE2D_DESC desc{};
    texture->GetDesc(&desc);
    if (size.Width <= 0 || size.Height <= 0 || size.Width > static_cast<int>(desc.Width) ||
        size.Height > static_cast<int>(desc.Height))
        throw std::runtime_error("Window changed size during capture; try again");
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    desc.MiscFlags = 0;
    com_ptr<ID3D11Texture2D> staging;
    check_hresult(device->CreateTexture2D(&desc, nullptr, staging.put()));
    context->CopyResource(staging.get(), texture.get());
    D3D11_MAPPED_SUBRESOURCE mapped{};
    check_hresult(context->Map(staging.get(), 0, D3D11_MAP_READ, 0, &mapped));
    try
    {
        save_png(path, size.Width, size.Height, mapped.RowPitch, static_cast<BYTE *>(mapped.pData));
    }
    catch (...)
    {
        context->Unmap(staging.get(), 0);
        throw;
    }
    context->Unmap(staging.get(), 0);
    frame.Close();
    session.Close();
    pool.Close();
    return {size.Width, size.Height};
}
LRESULT CALLBACK fixture_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    if (msg == WM_DESTROY)
    {
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}
int fixture()
{
    WNDCLASSW wc{};
    wc.lpfnWndProc = fixture_proc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"SvgshotFixture";
    wc.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
    RegisterClassW(&wc);
    HWND hwnd = CreateWindowW(wc.lpszClassName, L"svgshot Capture Fixture",
                              WS_OVERLAPPEDWINDOW | WS_VISIBLE, 50, 50, 450, 330, nullptr, nullptr,
                              wc.hInstance, nullptr);
    CreateWindowW(L"BUTTON", L"Add…", WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 20, 30, 100, 30, hwnd,
                  nullptr, wc.hInstance, nullptr);
    HWND check =
        CreateWindowW(L"BUTTON", L"Preserve semantics", WS_CHILD | WS_VISIBLE | BS_AUTOCHECKBOX, 20,
                      75, 220, 30, hwnd, nullptr, wc.hInstance, nullptr);
    SendMessageW(check, BM_SETCHECK, BST_CHECKED, 0);
    CreateWindowW(L"EDIT", L"Exact text: <SVG> & UIA",
                  WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL | ES_MULTILINE, 20, 120, 300,
                  30, hwnd, nullptr, wc.hInstance, nullptr);
    CreateWindowW(L"EDIT", L"PasswordHiddenSentinel",
                  WS_CHILD | WS_VISIBLE | WS_BORDER | ES_PASSWORD, 20, 165, 300, 30, hwnd, nullptr,
                  wc.hInstance, nullptr);
    LoadLibraryW(L"Msftedit.dll");
    CreateWindowW(L"RICHEDIT50W", L"Formatted visible text",
                  WS_CHILD | WS_VISIBLE | WS_BORDER | ES_MULTILINE, 20, 205, 300, 40, hwnd, nullptr,
                  wc.hInstance, nullptr);
    // Neither of these sentinel values is visible in the bitmap.
    CreateWindowW(L"EDIT", L"HiddenValueSentinel", WS_CHILD, 20, 10, 100, 20, hwnd, nullptr,
                  wc.hInstance, nullptr);
    CreateWindowW(L"EDIT", L"OutsideValueSentinel", WS_CHILD | WS_VISIBLE, 1000, 1000, 100, 20,
                  hwnd, nullptr, wc.hInstance, nullptr);
    MSG msg{};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0)
    {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    return 0;
}
int wmain(int argc, wchar_t **argv)
{
    try
    {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        winrt::init_apartment(winrt::apartment_type::multi_threaded);
        HWND hwnd = nullptr;
        std::wstring prefix;
        bool uiaOnly = false, foreground = false, jsonExport = false, includeHidden = false;
        int delay = 0;
        for (int i = 1; i < argc; ++i)
        {
            std::wstring arg = argv[i];
            if (arg == L"--fixture")
                return fixture();
            if (arg == L"--out" && i + 1 < argc)
                prefix = argv[++i];
            else if (arg == L"--hwnd" && i + 1 < argc)
                hwnd = reinterpret_cast<HWND>(std::stoull(argv[++i], nullptr, 0));
            else if (arg == L"--foreground")
                foreground = true;
            else if (arg == L"--delay" && i + 1 < argc)
                delay = std::stoi(argv[++i]);
            else if (arg == L"--include-hidden-content")
                includeHidden = true;
            else if (arg == L"--json")
                jsonExport = true;
            else if (arg == L"--uia-only")
                uiaOnly = true;
            else if (arg == L"--help")
            {
                std::cout << "svgshot-capture --out PREFIX [--hwnd NUMBER | --foreground --delay "
                             "SECONDS] [--json] [--uia-only] [--include-hidden-content]\nSelect a "
                             "window by clicking it; Esc "
                             "cancels. "
                             "Requires Windows 10 1903+.\n";
                return 0;
            }
            else
                throw std::runtime_error("Unknown or incomplete option");
        }
        if (prefix.empty())
            throw std::runtime_error("--out PREFIX is required");
        if (delay < 0 || delay > 60)
            throw std::runtime_error("Delay must be between 0 and 60 seconds");
        if (hwnd && foreground)
            throw std::runtime_error("Choose --hwnd or --foreground");
        if (delay)
            std::this_thread::sleep_for(std::chrono::seconds(delay));
        if (foreground)
            hwnd = GetForegroundWindow();
        if (!hwnd)
            hwnd = pick_window();
        if (!hwnd)
        {
            std::cout << "Capture cancelled\n";
            return 2;
        }
        if (!IsWindow(hwnd) || IsIconic(hwnd))
            throw std::runtime_error("Target is missing or minimized");
        RECT before = bounds(hwnd);
        auto snapshot = read_uia(hwnd, includeHidden);
        SIZE size{before.right - before.left, before.bottom - before.top};
        if (!uiaOnly)
            size = capture_png(hwnd, prefix + L".png");
        RECT after = bounds(hwnd);
        if (!EqualRect(&before, &after))
            throw std::runtime_error("Window moved or resized during capture; try again");
        // WGC and DWM normally agree; some window frames instead match GetWindowRect.
        RECT conventional{};
        GetWindowRect(hwnd, &conventional);
        if (size.cx == conventional.right - conventional.left &&
            size.cy == conventional.bottom - conventional.top)
            before = conventional;
        bool mapped =
            size.cx != before.right - before.left || size.cy != before.bottom - before.top;
        std::ostringstream out;
        out << "{\"version\":2,\"screen_bounds\":" << rect_json(before) << ",\"image_size\":["
            << size.cx << ',' << size.cy << "],\"warnings\":[";
        bool warning = false;
        if (snapshot->truncated)
        {
            out << "\"UIA tree truncated at 5000 elements or 64 levels\"";
            warning = true;
        }
        if (snapshot->text_truncated)
        {
            if (warning)
                out << ',';
            out << "\"Text capture truncated by line, format-run, selection, or string limit\"";
            warning = true;
        }
        if (mapped)
        {
            if (warning)
                out << ',';
            out << "\"Capture size differs from window bounds; coordinates require scaling. "
                   "Inspect the result.\"";
        }
        out << "],\"property_names\":" << snapshot->property_names
            << ",\"pattern_names\":" << snapshot->pattern_names
            << ",\"capture_policy\":{\"view\":\"raw\",\"property_ids\":[30000,30199],"
               "\"pattern_ids\":[10000,10034],\"text_attribute_ids\":[40000,40043],"
               "\"max_elements\":5000,\"max_depth\":64,\"max_lines_per_element\":2000,"
               "\"max_format_runs_per_element\":2048,\"max_selections_per_element\":256,\"max_"
               "string_characters\":65536,"
               "\"timeout_seconds\":20,\"custom_properties\":\"not_discovered\","
               "\"password_content\":\"redacted\",\"actions_invoked\":false,\"include_hidden_"
               "content\":"
            << (includeHidden ? "true" : "false") << "},\"root\":" << snapshot->root << "}\n";
        if (!uiaOnly)
            embed_snapshot(std::filesystem::path(prefix + L".png"), out.str());
        if (jsonExport || uiaOnly)
        {
            std::ofstream file(std::filesystem::path(prefix + L".uia.json"), std::ios::binary);
            file << out.str();
            file.close();
            if (!file)
                throw std::runtime_error("Cannot write UIA JSON export");
        }
        return 0;
    }
    catch (const winrt::hresult_error &e)
    {
        std::cerr << "svgshot-capture: " << utf8(e.message().c_str()) << '\n';
        return 1;
    }
    catch (const std::exception &e)
    {
        std::cerr << "svgshot-capture: " << e.what() << '\n';
        return 1;
    }
}
