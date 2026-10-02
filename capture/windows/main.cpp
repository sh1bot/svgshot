// UI Automation + Windows Graphics Capture + WIC PNG, with vendored miniz.
#include <windows.h>
#include <ole2.h>
#include <oleacc.h>
#include <richedit.h>
#include <shlobj.h>
#include <cwctype>
#include <UIAutomation.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <fcntl.h>
#include <io.h>
#include <condition_variable>
#include <d3d11.h>
#include <dwmapi.h>
#include <filesystem>
#include <functional>
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
#include "unified_snapshot.h"
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
std::string hresult_message(const winrt::hresult_error &error)
{
    char code[11];
    sprintf_s(code, "0x%08X", static_cast<unsigned int>(error.code().value));
    return std::string(code) + ": " + utf8(error.message().c_str());
}
// Preserve the failing operation and HRESULT; localized COM messages alone
// (especially E_INVALIDARG) cannot distinguish UIA, WGC, D3D and WIC failures.
template <typename F> auto capture_step(const char *step, F action) -> decltype(action())
{
    try
    {
        return action();
    }
    catch (const winrt::hresult_error &error)
    {
        throw std::runtime_error(std::string(step) + ": " + hresult_message(error));
    }
    catch (const std::exception &error)
    {
        throw std::runtime_error(std::string(step) + ": " + error.what());
    }
}
void check_step(HRESULT hr, const char *step)
{
    capture_step(step, [hr] { check_hresult(hr); });
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

HRESULT clip_to_visible_range(IUIAutomationTextRange *range, IUIAutomationTextRange *viewport)
{
    int cmp = 0;
    HRESULT hr = range->CompareEndpoints(TextPatternRangeEndpoint_Start, viewport,
                                         TextPatternRangeEndpoint_Start, &cmp);
    if (FAILED(hr))
        return hr;
    if (cmp < 0)
    {
        hr = range->MoveEndpointByRange(TextPatternRangeEndpoint_Start, viewport,
                                        TextPatternRangeEndpoint_Start);
        if (FAILED(hr))
            return hr;
    }
    hr = range->CompareEndpoints(TextPatternRangeEndpoint_End, viewport,
                                 TextPatternRangeEndpoint_End, &cmp);
    if (FAILED(hr))
        return hr;
    if (cmp > 0)
    {
        hr = range->MoveEndpointByRange(TextPatternRangeEndpoint_End, viewport,
                                        TextPatternRangeEndpoint_End);
        if (FAILED(hr))
            return hr;
    }
    hr = range->CompareEndpoints(TextPatternRangeEndpoint_Start, range,
                                 TextPatternRangeEndpoint_End, &cmp);
    return FAILED(hr) ? hr : cmp >= 0 ? S_FALSE : S_OK;
}

std::string range_details(IUIAutomationTextRange *range, UiaData &data)
{
    if (data.hidden_text(range))
        return "{\"text\":\"\",\"rectangles\":[],\"status\":\"redacted\",\"reason\":\"hidden_"
               "text\"}";
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
    hr = run->MoveEndpointByRange(TextPatternRangeEndpoint_End, run.get(),
                                  TextPatternRangeEndpoint_Start);
    if (FAILED(hr))
        return UiaData::failure(hr);
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
        {
            hr = run->MoveEndpointByRange(TextPatternRangeEndpoint_End, range,
                                          TextPatternRangeEndpoint_End);
            if (FAILED(hr))
                break;
        }
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
                if (clip_to_visible_range(range.get(), viewport.get()) != S_OK)
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
        HRESULT line_hr = line->MoveEndpointByRange(TextPatternRangeEndpoint_End, visible.get(),
                                                    TextPatternRangeEndpoint_Start);
        if (SUCCEEDED(line_hr))
            line_hr = line->ExpandToEnclosingUnit(TextUnit_Line);
        if (FAILED(line_hr))
        {
            status = UiaData::failure(line_hr);
            continue;
        }
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
            HRESULT clip_hr = clip_to_visible_range(clipped.get(), visible.get());
            if (FAILED(clip_hr))
            {
                status = UiaData::failure(clip_hr);
                break;
            }
            if (data.hidden_text(clipped.get()))
            {
                if (comma)
                    out << ',';
                comma = true;
                out << "{\"text\":\"\",\"rectangles\":[],\"status\":\"redacted\",\"reason\":"
                       "\"hidden_text\"}";
                int moved = 0;
                if (FAILED(line->Move(TextUnit_Line, 1, &moved)) || moved == 0)
                    break;
                continue;
            }
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
    bool debug_unredacted = false;
    Reader(RECT area, bool allow_hidden, bool debug)
        : viewport(area), include_hidden(allow_hidden || debug), debug_unredacted(debug)
    {
        check_hresult(CoCreateInstance(__uuidof(CUIAutomation), nullptr, CLSCTX_INPROC_SERVER,
                                       IID_PPV_ARGS(automation.put())));
        check_hresult(automation->get_RawViewWalker(walker.put()));
        data = std::make_unique<UiaData>(automation.get());
        data->include_hidden_content = include_hidden;
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
        if ((!password || debug_unredacted) && !redact_content)
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
        auto lines = (password && !debug_unredacted) || redact_content
                         ? "[]"
                         : text_ranges(e, *data, text_status);
        out << "},\"properties\":" << data->read_properties(
                   e, password, hidden, include_hidden, debug_unredacted)
            << ",\"patterns\":" << data->read_patterns(e) << ",\"text_ranges\":" << lines
            << ",\"text_capture\":" << text_status << ",\"text_selection\":"
            << ((password && !debug_unredacted) || redact_content ? "{\"status\":\"redacted\"}"
                                           : text_selection(e, *data, include_hidden))
            << ",\"children\":[";
        com_ptr<IUIAutomationElement> child;
        HRESULT children_hr = S_OK;
        if (!password || debug_unredacted)
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
    std::string root, error, property_names, pattern_names, provider = "windows-uia";
    std::vector<std::string> warnings;
    int count = 0;
    bool text_truncated = false;
    bool truncated = false;
    ~Snapshot()
    {
        CloseHandle(done);
    }
};
std::shared_ptr<Snapshot> read_uia(HWND hwnd, bool include_hidden, bool debug_unredacted)
{
    auto result = std::make_shared<Snapshot>();
    std::thread worker([hwnd, result, include_hidden, debug_unredacted] {
        try
        {
            winrt::init_apartment(winrt::apartment_type::multi_threaded);
            {
                Reader reader(bounds(hwnd), include_hidden, debug_unredacted);
                com_ptr<IUIAutomationElement> root;
                check_step(reader.automation->ElementFromHandle(hwnd, root.put()),
                           "UIA ElementFromHandle");
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
            result->error = hresult_message(e);
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

int msaa_control_type(const VARIANT &role)
{
    if (role.vt != VT_I4)
        return UIA_CustomControlTypeId;
    switch (role.lVal)
    {
    case ROLE_SYSTEM_WINDOW: return UIA_WindowControlTypeId;
    case ROLE_SYSTEM_CLIENT: return UIA_PaneControlTypeId;
    case ROLE_SYSTEM_PUSHBUTTON: return UIA_ButtonControlTypeId;
    case ROLE_SYSTEM_CHECKBUTTON: return UIA_CheckBoxControlTypeId;
    case ROLE_SYSTEM_RADIOBUTTON: return UIA_RadioButtonControlTypeId;
    case ROLE_SYSTEM_COMBOBOX: return UIA_ComboBoxControlTypeId;
    case ROLE_SYSTEM_TEXT: return UIA_EditControlTypeId;
    case ROLE_SYSTEM_STATICTEXT: return UIA_TextControlTypeId;
    case ROLE_SYSTEM_LINK: return UIA_HyperlinkControlTypeId;
    case ROLE_SYSTEM_LIST: return UIA_ListControlTypeId;
    case ROLE_SYSTEM_LISTITEM: return UIA_ListItemControlTypeId;
    case ROLE_SYSTEM_OUTLINE: return UIA_TreeControlTypeId;
    case ROLE_SYSTEM_OUTLINEITEM: return UIA_TreeItemControlTypeId;
    case ROLE_SYSTEM_MENUPOPUP: return UIA_MenuControlTypeId;
    case ROLE_SYSTEM_MENUITEM: return UIA_MenuItemControlTypeId;
    case ROLE_SYSTEM_MENUBAR: return UIA_MenuBarControlTypeId;
    case ROLE_SYSTEM_TOOLBAR: return UIA_ToolBarControlTypeId;
    case ROLE_SYSTEM_PAGETAB: return UIA_TabItemControlTypeId;
    case ROLE_SYSTEM_PROGRESSBAR: return UIA_ProgressBarControlTypeId;
    case ROLE_SYSTEM_SLIDER: return UIA_SliderControlTypeId;
    case ROLE_SYSTEM_SCROLLBAR: return UIA_ScrollBarControlTypeId;
    case ROLE_SYSTEM_SEPARATOR: return UIA_SeparatorControlTypeId;
    case ROLE_SYSTEM_TABLE: return UIA_TableControlTypeId;
    case ROLE_SYSTEM_ROW: return UIA_DataItemControlTypeId;
    case ROLE_SYSTEM_COLUMNHEADER: case ROLE_SYSTEM_ROWHEADER: return UIA_HeaderItemControlTypeId;
    default: return UIA_CustomControlTypeId;
    }
}

std::string read_msaa_node(IAccessible *accessible, VARIANT child, RECT viewport,
                           bool include_hidden, bool debug_unredacted, int depth,
                           int &count, bool &truncated)
{
    if (++count > 5000 || depth > 64)
    {
        truncated = true;
        return "null";
    }
    VARIANT name, value, role, state;
    VariantInit(&name); VariantInit(&value); VariantInit(&role); VariantInit(&state);
    HRESULT name_hr = accessible->get_accName(child, &name.bstrVal);
    if (SUCCEEDED(name_hr)) name.vt = VT_BSTR;
    HRESULT value_hr = accessible->get_accValue(child, &value.bstrVal);
    if (SUCCEEDED(value_hr)) value.vt = VT_BSTR;
    HRESULT role_hr = accessible->get_accRole(child, &role);
    HRESULT state_hr = accessible->get_accState(child, &state);
    LONG x = 0, y = 0, w = 0, h = 0;
    HRESULT bounds_hr = accessible->accLocation(&x, &y, &w, &h, child);
    RECT box{x, y, x + std::max<LONG>(0, w), y + std::max<LONG>(0, h)}, intersection{};
    bool offscreen = FAILED(bounds_hr) || w <= 0 || h <= 0 ||
                     !IntersectRect(&intersection, &box, &viewport);
    bool protected_content = role_hr == S_OK && role.vt == VT_I4 &&
                             role.lVal == ROLE_SYSTEM_TEXT && state_hr == S_OK &&
                             state.vt == VT_I4 && (state.lVal & STATE_SYSTEM_PROTECTED);
    bool redact = (offscreen && !include_hidden) || (protected_content && !debug_unredacted);
    std::wstring name_text = name.vt == VT_BSTR && name.bstrVal ? name.bstrVal : L"";
    std::wstring value_text = value.vt == VT_BSTR && value.bstrVal ? value.bstrVal : L"";
    std::wstring role_text;
    if (role.vt == VT_I4)
    {
        wchar_t buffer[128]{};
        UINT length = GetRoleTextW(static_cast<DWORD>(role.lVal), buffer, ARRAYSIZE(buffer));
        if (length) role_text.assign(buffer, length);
    }
    else if (role.vt == VT_BSTR && role.bstrVal)
        role_text = role.bstrVal;
    std::string id = "msaa-" + std::to_string(count);
    std::string result = "{\"id\":" + json(std::wstring(id.begin(), id.end())) +
        ",\"control_type\":" + std::to_string(role_hr == S_OK ? msaa_control_type(role) : UIA_CustomControlTypeId) +
        ",\"bounds\":" + rect_json(box) + ",\"name\":" + json(redact ? L"" : name_text) +
        ",\"localized_control_type\":" + json(role_text) + ",\"class_name\":\"IAccessible\",\"framework_id\":\"MSAA\"" +
        ",\"enabled\":" + std::string(state.vt == VT_I4 && !(state.lVal & STATE_SYSTEM_UNAVAILABLE) ? "true" : "false") +
        ",\"offscreen\":" + (offscreen ? "true" : "false") +
        ",\"password\":" + (protected_content ? "true" : "false") +
        ",\"content_redacted\":" + (redact ? "true" : "false") +
        ",\"states\":{\"selected\":" + std::string(state.vt == VT_I4 && (state.lVal & STATE_SYSTEM_SELECTED) ? "true" : "false");
    if (state.vt == VT_I4 && (state.lVal & STATE_SYSTEM_FOCUSED)) result += ",\"keyboard_focus\":true";
    if (state.vt == VT_I4 && (state.lVal & STATE_SYSTEM_FOCUSABLE)) result += ",\"focusable\":true";
    if (value.vt == VT_BSTR && (debug_unredacted || include_hidden) && !redact)
        result += ",\"value\":" + json(value_text);
    result += "},\"properties\":{\"30005\":{";
    if (name_hr == S_OK && !redact)
        result += "\"status\":\"value\",\"type\":8,\"value\":" + json(name_text);
    else
        result += "\"status\":\"redacted\"";
    result += "},\"30093\":{";
    if (value_hr == S_OK && (debug_unredacted || include_hidden) && !redact)
        result += "\"status\":\"value\",\"type\":8,\"value\":" + json(value_text);
    else
        result += "\"status\":\"redacted\",\"reason\":\"potential_hidden_content\"";
    result += "}},\"text_ranges\":[],\"text_capture\":{\"status\":\"not_supported\"},\"text_selection\":{\"status\":\"not_supported\"},\"children\":[";
    LONG child_count = 0;
    HRESULT child_count_hr = accessible->get_accChildCount(&child_count);
    if (child_count_hr == S_OK && child_count > 0 && (!protected_content || debug_unredacted))
    {
        LONG amount = std::min<LONG>(child_count, 5000);
        std::vector<VARIANT> children(amount);
        for (auto &v : children) VariantInit(&v);
        LONG obtained = 0;
        HRESULT children_hr = AccessibleChildren(accessible, 0, amount, children.data(), &obtained);
        if (SUCCEEDED(children_hr))
        {
            for (LONG i = 0; i < obtained && count < 5000; ++i)
            {
                std::string child_json;
                if (children[i].vt == VT_DISPATCH && children[i].pdispVal)
                {
                    com_ptr<IAccessible> child_accessible;
                    if (SUCCEEDED(children[i].pdispVal->QueryInterface(IID_PPV_ARGS(child_accessible.put()))))
                    {
                        VARIANT self; VariantInit(&self); self.vt = VT_I4; self.lVal = CHILDID_SELF;
                        child_json = read_msaa_node(child_accessible.get(), self, viewport, include_hidden,
                                                    debug_unredacted, depth + 1, count, truncated);
                    }
                }
                else if (children[i].vt == VT_I4)
                    child_json = read_msaa_node(accessible, children[i], viewport, include_hidden,
                                                debug_unredacted, depth + 1, count, truncated);
                if (!child_json.empty() && child_json != "null")
                {
                    if (result.back() != '[') result += ',';
                    result += child_json;
                }
                VariantClear(&children[i]);
            }
        }
        else
            truncated = true;
        if (child_count > amount) truncated = true;
    }
    result += "],\"children_status\":{\"status\":\"" +
              std::string(child_count_hr == S_OK ? "value" : "error") + "\"}}";
    VariantClear(&name); VariantClear(&value); VariantClear(&role); VariantClear(&state);
    return result;
}

std::shared_ptr<Snapshot> read_msaa(HWND hwnd, bool include_hidden, bool debug_unredacted,
                                   RECT viewport)
{
    auto result = std::make_shared<Snapshot>();
    result->provider = "windows-msaa";
    result->property_names = "{\"30005\":\"Name\",\"30093\":\"LegacyIAccessiblePattern.Value\"}";
    result->pattern_names = "{}";
    std::thread worker([hwnd, result, include_hidden, debug_unredacted, viewport] {
        try
        {
            winrt::init_apartment(winrt::apartment_type::multi_threaded);
            com_ptr<IAccessible> root;
            HRESULT hr = AccessibleObjectFromWindow(hwnd, static_cast<DWORD>(OBJID_CLIENT), IID_IAccessible,
                                                     root.put_void());
            if (FAILED(hr) || !root)
                hr = AccessibleObjectFromWindow(hwnd, static_cast<DWORD>(OBJID_WINDOW), IID_IAccessible,
                                                root.put_void());
            if (FAILED(hr) || !root)
                throw std::runtime_error("AccessibleObjectFromWindow returned no IAccessible object");
            VARIANT self; VariantInit(&self); self.vt = VT_I4; self.lVal = CHILDID_SELF;
            result->root = read_msaa_node(root.get(), self, viewport, include_hidden,
                                          debug_unredacted, 0, result->count, result->truncated);
            winrt::uninit_apartment();
        }
        catch (const winrt::hresult_error &e)
        {
            result->error = hresult_message(e);
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
        throw std::runtime_error("MSAA provider did not respond within 20 seconds");
    }
    worker.join();
    if (!result->error.empty())
        throw std::runtime_error(result->error);
    return result;
}

int accessibility_score(const std::string &raw)
{
    using namespace winrt::Windows::Data::Json;
    auto root = JsonObject::Parse(winrt::to_hstring(raw));
    std::function<int(JsonObject)> visit = [&](JsonObject n) {
        int score = 0;
        int type = static_cast<int>(n.GetNamedNumber(L"control_type", UIA_CustomControlTypeId));
        auto name = n.GetNamedString(L"name", L"");
        if (!name.empty() && type != UIA_WindowControlTypeId && type != UIA_PaneControlTypeId)
            score += 3;
        if (type == UIA_TextControlTypeId || type == UIA_EditControlTypeId)
            score += 1;
        for (auto line : n.GetNamedArray(L"text_ranges", winrt::Windows::Data::Json::JsonArray()))
            if (!line.GetObject().GetNamedString(L"text", L"").empty()) score += 4;
        for (auto child : n.GetNamedArray(L"children", JsonArray()))
            score += visit(child.GetObject());
        return score;
    };
    return visit(root);
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
std::string save_png(UINT width, UINT height, UINT stride, BYTE *pixels)
{
    com_ptr<IWICImagingFactory> factory;
    check_hresult(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
                                   IID_PPV_ARGS(factory.put())));
    com_ptr<IStream> stream;
    check_hresult(CreateStreamOnHGlobal(nullptr, TRUE, stream.put()));
    com_ptr<IWICBitmapEncoder> encoder;
    check_hresult(factory->CreateEncoder(GUID_ContainerFormatPng, nullptr, encoder.put()));
    check_hresult(encoder->Initialize(stream.get(), WICBitmapEncoderNoCache));
    com_ptr<IWICBitmapFrameEncode> frame;
    check_hresult(encoder->CreateNewFrame(frame.put(), nullptr));
    check_hresult(frame->Initialize(nullptr));
    check_step(frame->SetSize(width, height), "WIC SetSize");
    WICPixelFormatGUID format = GUID_WICPixelFormat32bppBGRA;
    check_step(frame->SetPixelFormat(&format), "WIC SetPixelFormat");
    if (format != GUID_WICPixelFormat32bppBGRA)
        throw std::runtime_error("PNG encoder does not support BGRA");
    check_step(frame->WritePixels(height, stride, stride * height, pixels), "WIC WritePixels");
    check_step(frame->Commit(), "WIC frame Commit");
    check_step(encoder->Commit(), "WIC encoder Commit");
    STATSTG stat{};
    check_hresult(stream->Stat(&stat, STATFLAG_NONAME));
    std::string png(static_cast<size_t>(stat.cbSize.QuadPart), '\0');
    LARGE_INTEGER zero{};
    check_hresult(stream->Seek(zero, STREAM_SEEK_SET, nullptr));
    ULONG read = 0;
    check_hresult(stream->Read(png.data(), static_cast<ULONG>(png.size()), &read));
    if (read != png.size())
        throw std::runtime_error("Cannot read PNG memory stream");
    return png;
}
SIZE capture_printwindow(HWND hwnd, std::string &png)
{
    DWORD affinity = 0;
    if (GetWindowDisplayAffinity(hwnd, &affinity) && affinity != WDA_NONE)
        throw std::runtime_error("Window excludes its content from capture");
    struct Result
    {
        HANDLE done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        SIZE size{};
        std::string png, error;
        ~Result() { if (done) CloseHandle(done); }
    };
    auto result = std::make_shared<Result>();
    if (!result->done)
        throw std::runtime_error("Cannot create compatibility capture event");
    // PrintWindow asks only this window to paint, rather than copying desktop
    // pixels that might belong to another application. It can block a provider.
    std::thread worker([hwnd, result] {
        struct Surface
        {
            HDC dc = CreateCompatibleDC(nullptr);
            HBITMAP bitmap = nullptr;
            HGDIOBJ old = nullptr;
            ~Surface()
            {
                if (old) SelectObject(dc, old);
                if (bitmap) DeleteObject(bitmap);
                if (dc) DeleteDC(dc);
            }
        } surface;
        bool initialized = false;
        try
        {
            winrt::init_apartment(winrt::apartment_type::multi_threaded);
            initialized = true;
            RECT r{};
            if (!GetWindowRect(hwnd, &r))
                throw std::runtime_error("Cannot read compatibility capture bounds");
            LONG w = r.right - r.left, h = r.bottom - r.top;
            if (w <= 0 || h <= 0 || static_cast<uint64_t>(w) * h > 64 * 1024 * 1024)
                throw std::runtime_error("Invalid compatibility capture dimensions");
            BITMAPINFO info{};
            info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
            info.bmiHeader.biWidth = w;
            info.bmiHeader.biHeight = -h;
            info.bmiHeader.biPlanes = 1;
            info.bmiHeader.biBitCount = 32;
            info.bmiHeader.biCompression = BI_RGB;
            void *pixels = nullptr;
            if (!surface.dc)
                throw std::runtime_error("Cannot create compatibility capture DC");
            surface.bitmap = CreateDIBSection(surface.dc, &info, DIB_RGB_COLORS, &pixels, nullptr, 0);
            if (!surface.bitmap || !pixels)
                throw std::runtime_error("Cannot allocate compatibility bitmap");
            surface.old = SelectObject(surface.dc, surface.bitmap);
            if (!surface.old || surface.old == HGDI_ERROR)
            {
                surface.old = nullptr;
                throw std::runtime_error("Cannot select compatibility bitmap");
            }
            auto data = static_cast<BYTE *>(pixels);
            std::fill_n(data, static_cast<size_t>(w) * h * 4, BYTE{255});
            if (!PrintWindow(hwnd, surface.dc, 0))
                throw std::runtime_error("PrintWindow failed (Windows error " +
                                         std::to_string(GetLastError()) + ")");
            // GDI drawing does not initialize the alpha channel.
            for (size_t i = 3; i < static_cast<size_t>(w) * h * 4; i += 4)
                data[i] = 255;
            result->png = capture_step("Encode compatibility PNG", [&] {
                return save_png(w, h, w * 4, data);
            });
            result->size = {w, h};
        }
        catch (const winrt::hresult_error &e) { result->error = hresult_message(e); }
        catch (const std::exception &e) { result->error = e.what(); }
        if (initialized) winrt::uninit_apartment();
        SetEvent(result->done);
    });
    if (WaitForSingleObject(result->done, 8000) != WAIT_OBJECT_0)
    {
        worker.detach();
        throw std::runtime_error("PrintWindow did not respond within 8 seconds");
    }
    worker.join();
    if (!result->error.empty()) throw std::runtime_error(result->error);
    png = std::move(result->png);
    return result->size;
}
void require_unobscured(HWND hwnd, RECT rectangle)
{
    // Screen fallback samples the desktop composition, so refuse overlapping
    // windows rather than embedding their pixels with this target's semantics.
    // Check on both sides of the copy; composition is not an atomic UI snapshot.
    DWORD cloaked = 0;
    DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, &cloaked, sizeof(cloaked));
    if (cloaked) throw std::runtime_error("Target is not visible on the current desktop");
    struct Visibility { HWND target; RECT rectangle; bool found = false, blocked = false; } state{hwnd, rectangle};
    EnumWindows([](HWND window, LPARAM parameter) -> BOOL {
        auto &s = *reinterpret_cast<Visibility *>(parameter);
        if (window == s.target) { s.found = true; return FALSE; }
        if (!IsWindowVisible(window) || IsIconic(window)) return TRUE;
        DWORD cloaked = 0;
        DwmGetWindowAttribute(window, DWMWA_CLOAKED, &cloaked, sizeof(cloaked));
        if (cloaked) return TRUE;
        RECT r{}, overlap{};
        if (FAILED(DwmGetWindowAttribute(window, DWMWA_EXTENDED_FRAME_BOUNDS, &r, sizeof(r))) &&
            !GetWindowRect(window, &r)) return TRUE;
        if (IntersectRect(&overlap, &r, &s.rectangle)) { s.blocked = true; return FALSE; }
        return TRUE;
    }, reinterpret_cast<LPARAM>(&state));
    if (state.blocked)
        throw std::runtime_error("Visible-screen capture requires an unobscured window; move overlapping windows away and try again");
    if (!state.found || !IsWindowVisible(hwnd) || IsIconic(hwnd))
        throw std::runtime_error("Target is not a visible top-level window");
}
SIZE capture_screen(HWND hwnd, std::string &png)
{
    DWORD affinity = 0;
    if (GetWindowDisplayAffinity(hwnd, &affinity) && affinity != WDA_NONE)
        throw std::runtime_error("Window excludes its content from capture");
    RECT r = bounds(hwnd);
    LONG w = r.right - r.left, h = r.bottom - r.top;
    RECT desktop{GetSystemMetrics(SM_XVIRTUALSCREEN), GetSystemMetrics(SM_YVIRTUALSCREEN), 0, 0};
    desktop.right = desktop.left + GetSystemMetrics(SM_CXVIRTUALSCREEN);
    desktop.bottom = desktop.top + GetSystemMetrics(SM_CYVIRTUALSCREEN);
    if (w <= 0 || h <= 0 || static_cast<uint64_t>(w) * h > 64 * 1024 * 1024 ||
        r.left < desktop.left || r.top < desktop.top || r.right > desktop.right || r.bottom > desktop.bottom)
        throw std::runtime_error("Visible-screen capture requires the whole window to be on screen");
    require_unobscured(hwnd, r);
    struct Surface
    {
        HDC screen = GetDC(nullptr), dc = nullptr;
        HBITMAP bitmap = nullptr;
        HGDIOBJ old = nullptr;
        ~Surface() {
            if (old) SelectObject(dc, old);
            if (bitmap) DeleteObject(bitmap);
            if (dc) DeleteDC(dc);
            if (screen) ReleaseDC(nullptr, screen);
        }
    } surface;
    if (!surface.screen || !(surface.dc = CreateCompatibleDC(surface.screen)))
        throw std::runtime_error("Cannot create screen capture DC");
    BITMAPINFO info{};
    info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    info.bmiHeader.biWidth = w; info.bmiHeader.biHeight = -h;
    info.bmiHeader.biPlanes = 1; info.bmiHeader.biBitCount = 32;
    info.bmiHeader.biCompression = BI_RGB;
    void *pixels = nullptr;
    surface.bitmap = CreateDIBSection(surface.dc, &info, DIB_RGB_COLORS, &pixels, nullptr, 0);
    if (!surface.bitmap || !pixels) throw std::runtime_error("Cannot allocate screen bitmap");
    surface.old = SelectObject(surface.dc, surface.bitmap);
    if (!surface.old || surface.old == HGDI_ERROR) {
        surface.old = nullptr; throw std::runtime_error("Cannot select screen bitmap");
    }
    check_step(DwmFlush(), "Flush desktop composition");
    if (!BitBlt(surface.dc, 0, 0, w, h, surface.screen, r.left, r.top, SRCCOPY | CAPTUREBLT))
        throw std::runtime_error("Screen BitBlt failed (Windows error " + std::to_string(GetLastError()) + ")");
    if (!GdiFlush()) throw std::runtime_error("Cannot flush screen bitmap");
    require_unobscured(hwnd, r);
    RECT after = bounds(hwnd);
    if (!EqualRect(&r, &after)) throw std::runtime_error("Window moved during screen capture; try again");
    auto data = static_cast<BYTE *>(pixels);
    for (size_t i = 3; i < static_cast<size_t>(w) * h * 4; i += 4) data[i] = 255;
    png = save_png(w, h, w * 4, data);
    return {w, h};
}
SIZE capture_png(HWND hwnd, std::string &png, bool &compatibility, bool &screenCapture)
{
    if (screenCapture) return capture_screen(hwnd, png);
    if (compatibility)
        return capture_printwindow(hwnd, png);
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
    hr = interop->CreateForWindow(hwnd, winrt::guid_of<capture::GraphicsCaptureItem>(),
                                 winrt::put_abi(item));
    if (hr == E_INVALIDARG)
    {
        wchar_t windowClass[256]{};
        GetClassNameW(hwnd, windowClass, 256);
        std::cerr << "svgshot-capture: WGC rejected window: class=" << utf8(windowClass)
                  << ", style=0x" << std::hex << GetWindowLongPtrW(hwnd, GWL_STYLE)
                  << ", exstyle=0x" << GetWindowLongPtrW(hwnd, GWL_EXSTYLE) << std::dec
                  << ", owned=" << (GetWindow(hwnd, GW_OWNER) != nullptr)
                  << ", valid=" << IsWindow(hwnd) << ", visible=" << IsWindowVisible(hwnd) << '\n';
        screenCapture = true;
        return capture_step("WGC CreateForWindow rejected this window (0x80070057); visible-screen fallback",
                            [&] { return capture_screen(hwnd, png); });
    }
    check_step(hr, "WGC CreateForWindow");
    auto pool = capture_step("WGC CreateFreeThreaded", [&] {
        return capture::Direct3D11CaptureFramePool::CreateFreeThreaded(
            runtimeDevice,
            winrt::Windows::Graphics::DirectX::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2,
            item.Size());
    });
    auto session = capture_step("WGC CreateCaptureSession", [&] {
        return pool.CreateCaptureSession(item);
    });
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
    capture_step("WGC StartCapture", [&] { session.StartCapture(); });
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
    check_step(access->GetInterface(__uuidof(ID3D11Texture2D), texture.put_void()),
               "D3D GetInterface");
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
    check_step(device->CreateTexture2D(&desc, nullptr, staging.put()), "D3D CreateTexture2D");
    context->CopyResource(staging.get(), texture.get());
    D3D11_MAPPED_SUBRESOURCE mapped{};
    check_step(context->Map(staging.get(), 0, D3D11_MAP_READ, 0, &mapped), "D3D Map");
    try
    {
        png = capture_step("Encode PNG", [&] {
            return save_png(size.Width, size.Height, mapped.RowPitch,
                            static_cast<BYTE *>(mapped.pData));
        });
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
    HWND rich = CreateWindowW(L"RICHEDIT50W", L"Formatted visible text HiddenTextSentinel",
                              WS_CHILD | WS_VISIBLE | WS_BORDER | ES_MULTILINE, 20, 205, 300, 40,
                              hwnd, nullptr, wc.hInstance, nullptr);
    CHARFORMAT2W hidden_format{};
    hidden_format.cbSize = sizeof(hidden_format);
    hidden_format.dwMask = CFM_HIDDEN;
    hidden_format.dwEffects = CFE_HIDDEN;
    SendMessageW(rich, EM_SETSEL, 23, -1);
    SendMessageW(rich, EM_SETCHARFORMAT, SCF_SELECTION, reinterpret_cast<LPARAM>(&hidden_format));
    SendMessageW(rich, EM_SETSEL, 0, 0);
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
std::filesystem::path desktop_capture_path(HWND hwnd)
{
    PWSTR folder = nullptr;
    check_step(SHGetKnownFolderPath(FOLDERID_Desktop, KF_FLAG_DEFAULT, nullptr, &folder),
               "Locate Desktop folder");
    std::filesystem::path desktop(folder);
    CoTaskMemFree(folder);
    // GetWindowText reads the caption of a foreign process without sending a
    // potentially blocking message to its controls.
    wchar_t caption[256]{};
    GetWindowTextW(hwnd, caption, 256);
    std::wstring title(caption);
    for (auto &c : title)
        if (c < 32 || std::wstring(L"<>:\"/\\|?*").find(c) != std::wstring::npos) c = L'_';
    if (title.size() > 100) title.resize(100);
    if (!title.empty() && title.back() >= 0xd800 && title.back() <= 0xdbff) title.pop_back();
    while (!title.empty() && (title.back() == L'.' || title.back() == L' ')) title.pop_back();
    if (title.empty()) title = L"Window";
    SYSTEMTIME now{};
    GetLocalTime(&now);
    wchar_t stamp[40];
    swprintf_s(stamp, L"%04u-%02u-%02u_%02u-%02u-%02u-%03u", now.wYear, now.wMonth,
               now.wDay, now.wHour, now.wMinute, now.wSecond, now.wMilliseconds);
    std::wstring stem = std::wstring(stamp) + L" - " + title;
    auto output = desktop / (stem + L".png");
    for (unsigned suffix = 2; std::filesystem::exists(output); ++suffix)
        output = desktop / (stem + L" (" + std::to_wstring(suffix) + L").png");
    return output;
}
int wmain(int argc, wchar_t **argv)
{
    if (argc == 2 && std::wstring(argv[1]) == L"--version")
    {
        std::cout << "svgshot-capture-win " << SVGSHOT_COMMIT << "\n";
        return 0;
    }
    try
    {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        winrt::init_apartment(winrt::apartment_type::multi_threaded);
        HWND hwnd = nullptr;
        std::filesystem::path outputPath;
        bool uiaOnly = false, foreground = false, jsonExport = false, includeHidden = false,
             debugUnredacted = false,
             stdoutPng = false, compatibility = false, screenCapture = false;
        std::wstring accessibilityApi = L"auto";
        int delay = 0;
        for (int i = 1; i < argc; ++i)
        {
            std::wstring arg = argv[i];
            if (arg == L"--fixture")
                return fixture();
            if (arg == L"--out" && i + 1 < argc)
                outputPath = argv[++i];
            else if (arg == L"--hwnd" && i + 1 < argc)
                hwnd = reinterpret_cast<HWND>(std::stoull(argv[++i], nullptr, 0));
            else if (arg == L"--foreground")
                foreground = true;
            else if (arg == L"--delay" && i + 1 < argc)
                delay = std::stoi(argv[++i]);
            else if (arg == L"--accessibility-api" && i + 1 < argc)
                accessibilityApi = argv[++i];
            else if (arg == L"--include-hidden-content")
                includeHidden = true;
            else if (arg == L"--debug-unredacted")
                debugUnredacted = true;
            else if (arg == L"--stdout")
                stdoutPng = true;
            else if (arg == L"--print-window")
                compatibility = true;
            else if (arg == L"--screen")
                screenCapture = true;
            else if (arg == L"--json")
                jsonExport = true;
            else if (arg == L"--uia-only")
                uiaOnly = true;
            else if (arg == L"--help")
            {
                std::cout << "svgshot-capture [FILE.png] [--hwnd NUMBER | --foreground --delay "
                             "SECONDS] [--json] [--uia-only] [--include-hidden-content] "
                             "[--debug-unredacted] [--accessibility-api auto|uia|msaa]\nSelect a "
                             "window by clicking it; Esc "
                             "cancels. "
                             "Without a filename, save a dated PNG on the Desktop. --out FILE.png "
                             "also sets the filename. --print-window uses compatibility capture.\n"
                             "--screen captures the visible area of an unobscured window.\n"
                             "Requires Windows 10 1903+. Use --version for the source commit.\n";
                return 0;
            }
            else if (!arg.empty() && arg[0] != L'-' && outputPath.empty())
                outputPath = arg;
            else
                throw std::runtime_error("Unknown or incomplete option");
        }
        bool automaticPath = outputPath.empty() && !stdoutPng;
        if (!outputPath.empty())
        {
            auto extension = outputPath.extension().wstring();
            std::transform(extension.begin(), extension.end(), extension.begin(),
                           [](wchar_t c) { return static_cast<wchar_t>(towlower(c)); });
            if (extension != L".png") throw std::runtime_error("Output filename must end in .png");
        }
        if (stdoutPng && (!outputPath.empty() || jsonExport || uiaOnly))
            throw std::runtime_error("--stdout cannot be combined with file output options");
        if (delay < 0 || delay > 60)
            throw std::runtime_error("Delay must be between 0 and 60 seconds");
        if (accessibilityApi != L"auto" && accessibilityApi != L"uia" && accessibilityApi != L"msaa")
            throw std::runtime_error("--accessibility-api must be auto, uia, or msaa");
        if (debugUnredacted)
            std::cerr << "WARNING: debug unredacted capture may include sensitive information, including passwords and offscreen content.\n";
        if (compatibility && screenCapture) throw std::runtime_error("Choose --print-window or --screen");
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
            std::cerr << "Capture cancelled\n";
            return 2;
        }
        if (!IsWindow(hwnd) || IsIconic(hwnd))
            throw std::runtime_error("Target is missing or minimized");
        if (automaticPath) outputPath = desktop_capture_path(hwnd);
        RECT before = capture_step("Read window bounds", [&] { return bounds(hwnd); });
        std::shared_ptr<Snapshot> snapshot;
        std::string apiSelectionWarning;
        if (accessibilityApi == L"uia")
            snapshot = capture_step("Read UI Automation", [&] {
                return read_uia(hwnd, includeHidden, debugUnredacted);
            });
        else if (accessibilityApi == L"msaa")
            snapshot = capture_step("Read Microsoft Active Accessibility", [&] {
                return read_msaa(hwnd, includeHidden || debugUnredacted, debugUnredacted, before);
            });
        else
        {
            std::shared_ptr<Snapshot> uia, msaa;
            std::string uiaError, msaaError;
            try { uia = read_uia(hwnd, includeHidden, debugUnredacted); }
            catch (const std::exception &e) { uiaError = e.what(); }
            int us = uia ? accessibility_score(uia->root) : -1;
            // Most modern UIA trees need no legacy probe. Try MSAA when UIA
            // failed or exposes too few named elements to describe the window.
            if (!uia || us < 12)
            {
                try { msaa = read_msaa(hwnd, includeHidden || debugUnredacted, debugUnredacted, before); }
                catch (const std::exception &e) { msaaError = e.what(); }
            }
            if (!uia && !msaa)
                throw std::runtime_error("Both UIA and MSAA failed (UIA: " + uiaError + "; MSAA: " + msaaError + ")");
            if (uia && msaa)
            {
                int ms = accessibility_score(msaa->root);
                snapshot = ms > us ? msaa : uia;
                apiSelectionWarning = "Automatic accessibility API selection chose " +
                    (snapshot == msaa ? std::string("MSAA") : std::string("UIA")) +
                    " (UIA score " + std::to_string(us) + ", MSAA score " + std::to_string(ms) + ").";
                std::cerr << "svgshot-capture: " << apiSelectionWarning << "\n";
            }
            else if (uia)
            {
                snapshot = uia;
                apiSelectionWarning = msaaError.empty()
                    ? "Automatic accessibility API selection used UIA because its tree was sufficiently detailed."
                    : "Automatic accessibility API selection used UIA; MSAA failed: " + msaaError;
            }
            else
            {
                snapshot = msaa;
                apiSelectionWarning = "Automatic accessibility API selection used MSAA because UIA failed: " + uiaError;
            }
        }
        SIZE size{before.right - before.left, before.bottom - before.top};
        std::string png;
        if (!uiaOnly)
            size = capture_step("Capture window bitmap", [&] { return capture_png(hwnd, png, compatibility, screenCapture); });
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
        if (screenCapture && !uiaOnly)
        {
            out << "\"Bitmap captured from the selected window's visible screen area.\"";
            warning = true;
            std::cerr << "svgshot-capture: using visible-screen capture\n";
        }
        if (compatibility && !uiaOnly)
        {
            if (warning) out << ',';
            out << "\"Bitmap captured with PrintWindow compatibility capture; inspect visual content.\"";
            warning = true;
            std::cerr << "svgshot-capture: using PrintWindow compatibility capture\n";
        }
        if (snapshot->truncated)
        {
            if (warning) out << ',';
            out << json(std::wstring(snapshot->provider == "windows-msaa"
                ? L"MSAA tree truncated at 5000 elements or 64 levels"
                : L"UIA tree truncated at 5000 elements or 64 levels"));
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
            warning = true;
        }
        if (!apiSelectionWarning.empty())
        {
            if (warning) out << ',';
            out << json(std::wstring(apiSelectionWarning.begin(), apiSelectionWarning.end()));
            warning = true;
        }
        if (debugUnredacted)
        {
            if (warning) out << ',';
            out << "\"Debug unredacted capture may include sensitive information, including passwords and offscreen content.\"";
            warning = true;
        }
        out << "],\"property_names\":" << snapshot->property_names
            << ",\"pattern_names\":" << snapshot->pattern_names
            << ",\"capture_policy\":{\"view\":\"raw\",\"property_ids\":[30000,30199],"
               "\"pattern_ids\":[10000,10034],\"text_attribute_ids\":[40000,40043],"
               "\"max_elements\":5000,\"max_depth\":64,\"max_lines_per_element\":2000,"
               "\"max_format_runs_per_element\":2048,\"max_selections_per_element\":256,\"max_"
               "string_characters\":65536,"
               "\"timeout_seconds\":20,\"custom_properties\":\"not_discovered\","
               "\"password_content\":\""
            << (debugUnredacted ? "included" : "redacted")
            << "\",\"debug_unredacted\":" << (debugUnredacted ? "true" : "false")
            << ",\"accessibility_api_requested\":" << json(accessibilityApi)
            << ",\"accessibility_api_selected\":" << json(snapshot->provider == "windows-msaa" ? L"msaa" : L"uia")
            << ",\"actions_invoked\":false,\"bitmap_method\":\""
            << (uiaOnly ? "none" : screenCapture ? "screen" : compatibility ? "printwindow" : "wgc")
            << "\",\"include_hidden_"
               "content\":"
            << (includeHidden || debugUnredacted ? "true" : "false") << "},\"root\":" << snapshot->root << "}\n";
        auto normalized = capture_step("Normalize UI Automation snapshot", [&] {
            return unified::normalize(out.str(), snapshot->provider);
        });
        if (!uiaOnly)
        {
            png = embedded_png(std::move(png), normalized);
            if (stdoutPng)
            {
                _setmode(_fileno(stdout), _O_BINARY);
                std::cout.write(png.data(), static_cast<std::streamsize>(png.size()));
                if (!std::cout)
                    throw std::runtime_error("Cannot write PNG to stdout");
            }
            else
                write_capture(outputPath, png, !automaticPath);
        }
        if (jsonExport || uiaOnly)
        {
            auto jsonPath = outputPath;
            jsonPath.replace_extension(L".uia.json");
            std::ofstream file(jsonPath, std::ios::binary);
            file << normalized;
            file.close();
            if (!file)
                throw std::runtime_error("Cannot write UIA JSON export");
        }
        if (!stdoutPng && !uiaOnly) std::cout << utf8(outputPath.wstring()) << '\n';
        return 0;
    }
    catch (const winrt::hresult_error &e)
    {
        std::cerr << "svgshot-capture: " << hresult_message(e) << '\n';
        return 1;
    }
    catch (const std::exception &e)
    {
        std::cerr << "svgshot-capture: " << e.what() << '\n';
        return 1;
    }
}
