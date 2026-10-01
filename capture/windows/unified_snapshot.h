#pragma once
#include <winrt/Windows.Data.Json.h>
#include <winrt/Windows.Foundation.Collections.h>
#include <map>
namespace unified
{
using namespace winrt::Windows::Data::Json;
using winrt::hstring;
JsonValue string(const std::string &s)
{
    return JsonValue::CreateStringValue(winrt::to_hstring(s));
}
JsonValue number(double n)
{
    return JsonValue::CreateNumberValue(n);
}
JsonObject object()
{
    return JsonObject();
}
JsonObject status(const char *s)
{
    auto o = object();
    o.Insert(L"status", string(s));
    return o;
}
JsonObject style(JsonObject attrs)
{
    auto o = object();
    const std::map<std::wstring, std::wstring> names = {
        {L"40005", L"font_family"}, {L"40006", L"font_size"},    {L"40007", L"font_weight"},
        {L"40014", L"italic"},      {L"40015", L"hidden"},       {L"40018", L"read_only"},
        {L"40031", L"underline"},   {L"40026", L"strikethrough"}};
    for (auto const &p : attrs)
    {
        auto a = p.Value().GetObject();
        if (a.GetNamedString(L"status", L"") != L"value" || !a.HasKey(L"value"))
            continue;
        auto key = p.Key();
        if (key == L"40001" || key == L"40008")
        {
            auto v = a.GetNamedValue(L"value");
            if (v.ValueType() != JsonValueType::Number)
                continue;
            double d = v.GetNumber();
            if (d < 0 || d > 0xffffff || d != std::floor(d))
                continue;
            unsigned n = static_cast<unsigned>(d);
            char color[8];
            std::snprintf(color, sizeof(color), "#%02x%02x%02x", n & 255, (n >> 8) & 255,
                          (n >> 16) & 255);
            o.Insert(key == L"40001" ? L"background" : L"foreground", string(color));
        }
        else if (auto i = names.find(std::wstring(key)); i != names.end())
        {
            if (key == L"40031" || key == L"40026")
                o.Insert(i->second,
                         JsonValue::CreateBooleanValue(a.GetNamedNumber(L"value", 0) != 0));
            else
                o.Insert(i->second, a.GetNamedValue(L"value"));
            if (key == L"40006")
                o.Insert(L"font_size_unit", string("pt"));
        }
    }
    return o;
}
struct Builder
{
    std::map<std::wstring, std::string> ids;
    double sx, sy, scaleX, scaleY;
    void index(JsonObject n)
    {
        auto id = std::wstring(n.GetNamedString(L"id", L""));
        if (!ids.contains(id))
            ids[id] = "n" + std::to_string(ids.size() + 1);
        for (auto c : n.GetNamedArray(L"children", JsonArray()))
            index(c.GetObject());
    }
    JsonArray rect(JsonArray b)
    {
        JsonArray r;
        double x = b.GetNumberAt(0), y = b.GetNumberAt(1);
        r.Append(number((x - sx) * scaleX));
        r.Append(number((y - sy) * scaleY));
        r.Append(number(std::max(0.0, b.GetNumberAt(2) * scaleX)));
        r.Append(number(std::max(0.0, b.GetNumberAt(3) * scaleY)));
        return r;
    }
    JsonObject text(JsonObject n)
    {
        auto o = object();
        o.Insert(L"content", n.GetNamedValue(L"text", string("")));
        JsonArray rects;
        for (auto b : n.GetNamedArray(L"rectangles", JsonArray()))
            rects.Append(rect(b.GetArray()));
        o.Insert(L"rectangles", rects);
        o.Insert(L"style", style(n.GetNamedObject(L"attributes", object())));
        JsonArray runs;
        for (auto r :
             n.GetNamedObject(L"format_runs", object()).GetNamedArray(L"ranges", JsonArray()))
            runs.Append(text(r.GetObject()));
        o.Insert(L"runs", runs);
        return o;
    }
    JsonObject node(JsonObject n, JsonObject propertyNames)
    {
        static const std::vector<std::string> roles = {
            "button",      "calendar",    "checkbox",  "combobox",     "edit",     "hyperlink",
            "image",       "listitem",    "list",      "menu",         "menubar",  "menuitem",
            "progressbar", "radiobutton", "scrollbar", "slider",       "spinner",  "statusbar",
            "tab",         "tabitem",     "text",      "toolbar",      "tooltip",  "tree",
            "treeitem",    "custom",      "group",     "thumb",        "datagrid", "dataitem",
            "document",    "splitbutton", "window",    "pane",         "header",   "headeritem",
            "table",       "titlebar",    "separator", "semanticzoom", "appbar"};
        auto o = object();
        auto nativeId = n.GetNamedString(L"id", L"");
        o.Insert(L"id", string(ids[std::wstring(nativeId)]));
        int role = static_cast<int>(n.GetNamedNumber(L"control_type", 0)) - 50000;
        o.Insert(L"role", string(role >= 0 && role < static_cast<int>(roles.size()) ? roles[role]
                                                                                    : "custom"));
        o.Insert(L"native_ref", JsonValue::CreateStringValue(nativeId));
        o.Insert(L"bounds", rect(n.GetNamedArray(L"bounds")));
        for (auto const &p :
             std::map<std::wstring, std::wstring>{{L"name", L"label"},
                                                  {L"help_text", L"help"},
                                                  {L"localized_control_type", L"role_description"}})
            o.Insert(p.second, n.GetNamedValue(p.first, string("")));
        auto hints = object();
        hints.Insert(L"toolkit", n.GetNamedValue(L"framework_id", string("")));
        hints.Insert(L"class", n.GetNamedValue(L"class_name", string("")));
        o.Insert(L"hints", hints);
        auto states = object();
        for (auto const &p : std::map<std::wstring, std::wstring>{{L"enabled", L"enabled"},
                                                                  {L"keyboard_focus", L"focused"},
                                                                  {L"focusable", L"focusable"},
                                                                  {L"password", L"protected"},
                                                                  {L"offscreen", L"offscreen"}})
            if (n.HasKey(p.first))
                states.Insert(p.second, n.GetNamedValue(p.first));
        auto old = n.GetNamedObject(L"states", object());
        for (auto k : {L"selected", L"read_only"})
            if (old.HasKey(k))
                states.Insert(k, old.GetNamedValue(k));
        if (old.HasKey(L"toggle"))
        {
            int v = static_cast<int>(old.GetNamedNumber(L"toggle"));
            states.Insert(L"checked", string(v == 0   ? "unchecked"
                                             : v == 1 ? "checked"
                                             : v == 2 ? "mixed"
                                                      : "unknown"));
        }
        if (old.HasKey(L"expand_collapse"))
        {
            int v = static_cast<int>(old.GetNamedNumber(L"expand_collapse"));
            states.Insert(L"expansion", string(v == 0   ? "collapsed"
                                               : v == 1 ? "expanded"
                                               : v == 2 ? "partial"
                                               : v == 3 ? "leaf"
                                                        : "unknown"));
        }
        auto fieldStatus = object();
        auto normalizedBounds = o.GetNamedArray(L"bounds");
        if (normalizedBounds.GetNumberAt(2) == 0 || normalizedBounds.GetNumberAt(3) == 0)
            fieldStatus.Insert(L"bounds", status("not_captured"));
        for (auto const &p : n.GetNamedObject(L"properties", object()))
        {
            auto name = propertyNames.GetNamedString(p.Key(), L"");
            auto record = p.Value().GetObject();
            for (auto const &f :
                 std::map<std::wstring, std::wstring>{{L"IsEnabled", L"enabled"},
                                                      {L"HasKeyboardFocus", L"focused"},
                                                      {L"IsKeyboardFocusable", L"focusable"},
                                                      {L"IsOffscreen", L"offscreen"}})
            {
                std::wstring suffix = f.first + L"Property", s(name);
                if ((s.ends_with(suffix) || s.ends_with(f.first)) &&
                    record.GetNamedString(L"status", L"") != L"value")
                {
                    if (states.HasKey(f.second))
                        states.Remove(f.second);
                    fieldStatus.Insert(L"states." + f.second, record);
                }
            }
        }
        if (fieldStatus.Size())
            o.Insert(L"field_status", fieldStatus);
        o.Insert(L"states", states);
        auto value = status("not_captured");
        for (auto k : {L"value", L"range_value"})
            if (old.HasKey(k))
            {
                value = status("value");
                value.Insert(L"value", old.GetNamedValue(k));
            }
        auto t = status(winrt::to_string(n.GetNamedObject(L"text_capture", object())
                                             .GetNamedString(L"status", L"not_captured"))
                            .c_str());
        auto selection = n.GetNamedObject(L"text_selection", object());
        auto selections =
            status(winrt::to_string(selection.GetNamedString(L"status", L"not_captured")).c_str());
        if (selection.GetNamedString(L"status", L"") == L"value")
        {
            JsonArray selected;
            for (auto r : selection.GetNamedArray(L"ranges", JsonArray()))
                selected.Append(text(r.GetObject()));
            selections.Insert(L"value", selected);
        }
        t.Insert(L"selections", selections);
        JsonArray lines;
        for (auto r : n.GetNamedArray(L"text_ranges", JsonArray()))
            lines.Append(text(r.GetObject()));
        bool redacted =
            n.GetNamedBoolean(L"password", false) || n.GetNamedBoolean(L"content_redacted", false);
        if (redacted)
        {
            t = status("redacted");
            lines = JsonArray();
            value = status("redacted");
        }
        t.Insert(L"lines", lines);
        o.Insert(L"text", t);
        o.Insert(L"value", value);
        auto rel = object();
        auto label = n.GetNamedString(L"labeled_by", L"");
        auto found = ids.find(std::wstring(label));
        if (found != ids.end())
        {
            JsonArray labels;
            labels.Append(string(found->second));
            rel.Insert(L"labelled_by", labels);
        }
        o.Insert(L"relationships", rel);
        auto shortcuts = object();
        for (auto k : {L"access_key", L"accelerator_key"})
            if (n.GetNamedString(k, L"").size())
                shortcuts.Insert(k, n.GetNamedValue(k));
        o.Insert(L"shortcuts", shortcuts);
        JsonArray children;
        for (auto c : n.GetNamedArray(L"children", JsonArray()))
            children.Append(node(c.GetObject(), propertyNames));
        o.Insert(L"children", children);
        return o;
    }
};
std::string normalize(const std::string &raw)
{
    auto s = JsonObject::Parse(winrt::to_hstring(raw));
    auto b = s.GetNamedArray(L"screen_bounds"), size = s.GetNamedArray(L"image_size");
    Builder builder;
    builder.sx = b.GetNumberAt(0);
    builder.sy = b.GetNumberAt(1);
    builder.scaleX = size.GetNumberAt(0) / b.GetNumberAt(2);
    builder.scaleY = size.GetNumberAt(1) / b.GetNumberAt(3);
    builder.index(s.GetNamedObject(L"root"));
    auto out = object();
    out.Insert(L"format", string("svgshot.capture"));
    out.Insert(L"version", number(3));
    auto source = object();
    source.Insert(L"platform", string("windows"));
    source.Insert(L"provider", string("windows-uia"));
    out.Insert(L"source", source);
    auto image = object();
    image.Insert(L"size", size);
    image.Insert(L"coordinate_space", string("image-pixels"));
    image.Insert(L"source_bounds", b);
    JsonArray matrix;
    for (double v : {builder.scaleX, 0.0, 0.0, builder.scaleY, -builder.sx * builder.scaleX,
                     -builder.sy * builder.scaleY})
        matrix.Append(number(v));
    image.Insert(L"source_to_image", matrix);
    out.Insert(L"image", image);
    out.Insert(L"capture_policy", s.GetNamedObject(L"capture_policy"));
    out.Insert(L"warnings", s.GetNamedArray(L"warnings"));
    out.Insert(L"root",
               builder.node(s.GetNamedObject(L"root"), s.GetNamedObject(L"property_names")));
    auto native = object();
    native.Insert(L"provider", string("windows-uia"));
    native.Insert(L"snapshot", s);
    out.Insert(L"native", native);
    return winrt::to_string(out.Stringify());
}
} // namespace unified
