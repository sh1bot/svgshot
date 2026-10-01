#pragma once
#include <cmath>
#include <iomanip>

// Preserve typed values and failures separately from renderer convenience fields.
// No UIA actions are called and references are IDs, never recursive traversals.
struct UiaData
{
    IUIAutomation *automation;
    std::vector<int> properties, patterns;
    std::string property_names = "{", pattern_names = "{";
    bool text_truncated = false;
    explicit UiaData(IUIAutomation *a) : automation(a)
    {
        auto registry = [&](int first, int last, auto getter, auto &ids, auto &names) {
            for (int id = first; id <= last; ++id)
            {
                BSTR raw = nullptr;
                HRESULT hr = (automation->*getter)(id, &raw);
                auto name = bstr(raw);
                if (FAILED(hr) || name.empty())
                    continue;
                if (!ids.empty())
                    names += ',';
                ids.push_back(id);
                names += '"' + std::to_string(id) + "\":" + json(name);
            }
            names += '}';
        };
        registry(30000, 30199, &IUIAutomation::GetPropertyProgrammaticName, properties,
                 property_names);
        registry(10000, 10034, &IUIAutomation::GetPatternProgrammaticName, patterns, pattern_names);
    }
    static std::string failure(HRESULT hr)
    {
        std::ostringstream out;
        out << "{\"status\":\"error\",\"hresult\":\"0x" << std::hex << std::setw(8)
            << std::setfill('0') << static_cast<unsigned long>(hr) << "\"}";
        return out.str();
    }
    std::string value(VARIANT &v)
    {
        BOOL unsupported = FALSE;
        automation->CheckNotSupported(v, &unsupported);
        if (unsupported)
            return "{\"status\":\"not_supported\"}";
        if (v.vt == VT_UNKNOWN && v.punkVal)
        {
            com_ptr<IUnknown> mixed, identity;
            automation->get_ReservedMixedAttributeValue(mixed.put());
            v.punkVal->QueryInterface(IID_PPV_ARGS(identity.put()));
            if (mixed && identity.get() == mixed.get())
                return "{\"status\":\"mixed\"}";
        }
        std::string data;
        if (v.vt & VT_ARRAY)
        {
            if (!v.parray || SafeArrayGetDim(v.parray) != 1)
                return "{\"status\":\"unserialized\",\"type\":" + std::to_string(v.vt) + '}';
            LONG lo = 0, hi = -1;
            SafeArrayGetLBound(v.parray, 1, &lo);
            SafeArrayGetUBound(v.parray, 1, &hi);
            data = "[";
            for (LONG i = lo; i <= hi && i - lo < 5000; ++i)
            {
                VARIANT entry;
                VariantInit(&entry);
                entry.vt = v.vt & VT_TYPEMASK;
                void *target = nullptr;
                switch (entry.vt)
                {
                case VT_VARIANT:
                    target = &entry;
                    break;
                case VT_I4:
                    target = &entry.lVal;
                    break;
                case VT_UI4:
                    target = &entry.ulVal;
                    break;
                case VT_I8:
                    target = &entry.llVal;
                    break;
                case VT_UI8:
                    target = &entry.ullVal;
                    break;
                case VT_R8:
                    target = &entry.dblVal;
                    break;
                case VT_R4:
                    target = &entry.fltVal;
                    break;
                case VT_BOOL:
                    target = &entry.boolVal;
                    break;
                case VT_BSTR:
                    target = &entry.bstrVal;
                    break;
                case VT_UNKNOWN:
                    target = &entry.punkVal;
                    break;
                default:
                    break;
                }
                HRESULT hr = target ? SafeArrayGetElement(v.parray, &i, target) : E_NOTIMPL;
                if (i > lo)
                    data += ',';
                data += FAILED(hr) ? failure(hr) : value(entry);
                VariantClear(&entry);
            }
            data += ']';
            return "{\"status\":\"value\",\"type\":" + std::to_string(v.vt) + ",\"value\":" + data +
                   ",\"truncated\":" + (hi - lo >= 5000 ? "true}" : "false}");
        }
        std::ostringstream number;
        number << std::setprecision(17);
        switch (v.vt)
        {
        case VT_EMPTY:
        case VT_NULL:
            data = "null";
            break;
        case VT_BSTR:
            data = json(v.bstrVal ? std::wstring(v.bstrVal, SysStringLen(v.bstrVal)) : L"");
            break;
        case VT_BOOL:
            data = v.boolVal ? "true" : "false";
            break;
        case VT_I2:
            number << v.iVal;
            break;
        case VT_I4:
            number << v.lVal;
            break;
        case VT_UI2:
            number << v.uiVal;
            break;
        case VT_UI4:
            number << v.ulVal;
            break;
        case VT_I8:
            number << v.llVal;
            break;
        case VT_UI8:
            number << v.ullVal;
            break;
        case VT_R4:
            if (!std::isfinite(v.fltVal))
                return "{\"status\":\"non_finite\"}";
            number << v.fltVal;
            break;
        case VT_R8:
            if (!std::isfinite(v.dblVal))
                return "{\"status\":\"non_finite\"}";
            number << v.dblVal;
            break;
        case VT_UNKNOWN: {
            if (!v.punkVal)
            {
                data = "null";
                break;
            }
            com_ptr<IUIAutomationElement> e;
            com_ptr<IUIAutomationElementArray> array;
            if (SUCCEEDED(v.punkVal->QueryInterface(IID_PPV_ARGS(e.put()))))
                data = "{\"element\":\"" + runtime_id(e.get()) + "\"}";
            else if (SUCCEEDED(v.punkVal->QueryInterface(IID_PPV_ARGS(array.put()))))
            {
                int count = 0;
                HRESULT hr = array->get_Length(&count);
                if (FAILED(hr))
                    return failure(hr);
                data = "{\"elements\":[";
                for (int i = 0; i < count && i < 5000; ++i)
                {
                    com_ptr<IUIAutomationElement> child;
                    hr = array->GetElement(i, child.put());
                    if (i)
                        data += ',';
                    data += SUCCEEDED(hr) && child ? '"' + runtime_id(child.get()) + '"' : "null";
                }
                data += "],\"truncated\":" + std::string(count > 5000 ? "true}" : "false}");
            }
            else
                return "{\"status\":\"unserialized\",\"type\":13}";
            break;
        }
        default:
            return "{\"status\":\"unserialized\",\"type\":" + std::to_string(v.vt) + '}';
        }
        if (data.empty())
            data = number.str();
        return "{\"status\":\"value\",\"type\":" + std::to_string(v.vt) + ",\"value\":" + data +
               '}';
    }
    std::string read_properties(IUIAutomationElement *e, bool password)
    {
        std::string out = "{";
        bool comma = false;
        for (int id : properties)
        {
            if (comma)
                out += ',';
            comma = true;
            out += '"' + std::to_string(id) + "\":";
            // Value/Legacy value may reveal password contents. Other unknown
            // provider-specific fields are not probed on a password element.
            if (password && id != UIA_NamePropertyId && id != UIA_ControlTypePropertyId &&
                id != UIA_IsPasswordPropertyId && id != UIA_BoundingRectanglePropertyId &&
                id != UIA_IsEnabledPropertyId && id != UIA_HasKeyboardFocusPropertyId &&
                id != UIA_IsKeyboardFocusablePropertyId && id != UIA_IsOffscreenPropertyId)
            {
                out += "{\"status\":\"redacted\"}";
                continue;
            }
            VARIANT v;
            VariantInit(&v);
            HRESULT hr = e->GetCurrentPropertyValueEx(id, TRUE, &v);
            out += FAILED(hr) ? failure(hr) : value(v);
            VariantClear(&v);
        }
        return out + '}';
    }
    std::string read_patterns(IUIAutomationElement *e)
    {
        std::string out = "{";
        bool comma = false;
        for (int id : patterns)
        {
            com_ptr<IUnknown> p;
            HRESULT hr = e->GetCurrentPattern(id, p.put());
            if (comma)
                out += ',';
            comma = true;
            out += '"' + std::to_string(id) + "\":";
            out += FAILED(hr)
                       ? failure(hr)
                       : std::string("{\"status\":\"") + (p ? "supported\"}" : "not_supported\"}");
        }
        return out + '}';
    }
    std::string attributes(IUIAutomationTextRange *range)
    {
        std::string out = "{";
        for (int id = 40000; id <= 40043; ++id)
        {
            if (id != 40000)
                out += ',';
            VARIANT v;
            VariantInit(&v);
            HRESULT hr = range->GetAttributeValue(id, &v);
            out += '"' + std::to_string(id) + "\":" + (FAILED(hr) ? failure(hr) : value(v));
            VariantClear(&v);
        }
        return out + '}';
    }
};
