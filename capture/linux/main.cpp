// Native, read-only AT-SPI capture. No Python runtime or action invocation.
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <algorithm>
#include <array>
#include <atspi/atspi.h>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <json-c/json.h>
#include <map>
#include <memory>
#include <png.h>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>
#include <zlib.h>

#ifndef SVGSHOT_COMMIT
#define SVGSHOT_COMMIT "unknown"
#endif

struct J {
  std::shared_ptr<json_object> p;
  explicit J(json_object *v) : p(v, json_object_put) {}
  J() : J(static_cast<json_object *>(nullptr)) {}
  J(const char *v) : J(json_object_new_string(v)) {}
  J(const std::string &v)
      : J(json_object_new_string_len(v.data(), int(v.size()))) {}
  J(bool v) : J(json_object_new_boolean(v)) {}
  J(int v) : J(json_object_new_int(v)) {}
  J(double v) : J(json_object_new_double(v)) {}
  static J object() { return J(json_object_new_object()); }
  static J array() { return J(json_object_new_array()); }
  J &set(const std::string &k, const J &v) {
    json_object_object_add(p.get(), k.c_str(), json_object_get(v.p.get()));
    return *this;
  }
  void add(const J &v) {
    json_object_array_add(p.get(), json_object_get(v.p.get()));
  }
  J get(const char *k) const {
    json_object *v = nullptr;
    json_object_object_get_ex(p.get(), k, &v);
    return J(json_object_get(v));
  }
  std::string str() const {
    const char *s = json_object_get_string(p.get());
    return s ? s : "";
  }
  std::string dump() const {
    return json_object_to_json_string_ext(p.get(), JSON_C_TO_STRING_PLAIN);
  }
};
J obs(const char *s) { return J::object().set("status", s); }
J value(const J &v) { return obs("value").set("value", v); }
struct Error {
  GError *p = nullptr;
  ~Error() { g_clear_error(&p); }
  J record() const { return obs("error").set("code", p ? int(p->code) : -1); }
};
template <class T> using GPtr = std::unique_ptr<T, void (*)(gpointer)>;
template <class T> GPtr<T> own(T *p) { return GPtr<T>(p, g_object_unref); }
using Rect = std::array<double, 4>;
J rectjson(const Rect &r) {
  J a = J::array();
  for (auto x : r)
    a.add(x);
  return a;
}
Rect rect(AtspiRect *r) {
  return r ? Rect{double(r->x), double(r->y), double(std::max(0, r->width)),
                  double(std::max(0, r->height))}
           : Rect{0, 0, 0, 0};
}
bool intersects(const Rect &a, const Rect &b) {
  return a[2] > 0 && a[3] > 0 && a[0] < b[0] + b[2] && a[0] + a[2] > b[0] &&
         a[1] < b[1] + b[3] && a[1] + a[3] > b[1];
}
std::string bounded(const char *s) {
  if (!s)
    return "";
  const char *end =
      g_utf8_offset_to_pointer(s, std::min<glong>(65536, g_utf8_strlen(s, -1)));
  return std::string(s, end);
}
J string_record(char *s, Error &e) {
  J r = e.p ? e.record() : s ? value(bounded(s)) : obs("not_supported");
  g_free(s);
  return r;
}
Rect extents(AtspiAccessible *a, AtspiCoordType coords, J *record = nullptr) {
  auto component = own(atspi_accessible_get_component_iface(a));
  Error e;
  AtspiRect *r =
      component ? atspi_component_get_extents(component.get(), coords, &e.p)
                : nullptr;
  Rect b = rect(r);
  g_free(r);
  if (record)
    *record = e.p         ? e.record()
              : component ? value(rectjson(b))
                          : obs("not_supported");
  return b;
}
std::string normalized(std::string s) {
  std::replace(s.begin(), s.end(), ' ', '_');
  std::replace(s.begin(), s.end(), '-', '_');
  return s;
}
std::string role_name(const std::string &s) {
  static const std::map<std::string, std::string> roles = {
      {"push button", "button"},
      {"toggle button", "button"},
      {"check box", "checkbox"},
      {"radio button", "radiobutton"},
      {"text", "edit"},
      {"password text", "edit"},
      {"label", "text"},
      {"static", "text"},
      {"entry", "edit"},
      {"frame", "window"},
      {"dialog", "window"},
      {"panel", "pane"},
      {"filler", "pane"},
      {"section", "group"},
      {"combo box", "combobox"},
      {"page tab", "tabitem"},
      {"page tab list", "tab"},
      {"scroll bar", "scrollbar"},
      {"progress bar", "progressbar"},
      {"spin button", "spinner"},
      {"menu item", "menuitem"},
      {"menu bar", "menubar"},
      {"list item", "listitem"},
      {"tree item", "treeitem"},
      {"table cell", "dataitem"},
      {"table row", "dataitem"},
      {"table column header", "headeritem"},
      {"table row header", "headeritem"},
      {"tool bar", "toolbar"},
      {"status bar", "statusbar"},
      {"document frame", "document"},
      {"document text", "document"},
      {"link", "hyperlink"}};
  auto it = roles.find(s);
  return it == roles.end() ? normalized(s.empty() ? "custom" : s) : it->second;
}
struct CaptureWindow {
  std::string id, application, title;
  GPtr<AtspiAccessible> node;
};
std::vector<CaptureWindow> windows() {
  std::vector<CaptureWindow> out;
  auto desktop = own(atspi_get_desktop(0));
  if (!desktop)
    throw std::runtime_error("No AT-SPI desktop; enable desktop accessibility");
  Error de;
  int count = atspi_accessible_get_child_count(desktop.get(), &de.p);
  if (de.p)
    throw std::runtime_error("Cannot enumerate AT-SPI desktop");
  for (int i = 0; i < std::min(count, 1000); i++) {
    Error e;
    auto app = own(atspi_accessible_get_child_at_index(desktop.get(), i, &e.p));
    if (!app || e.p)
      continue;
    Error ae;
    auto appname =
        string_record(atspi_accessible_get_name(app.get(), &ae.p), ae)
            .get("value")
            .str();
    Error ce;
    int n = atspi_accessible_get_child_count(app.get(), &ce.p);
    if (ce.p)
      continue;
    for (int j = 0; j < std::min(n, 1000); j++) {
      Error ne;
      auto a = own(atspi_accessible_get_child_at_index(app.get(), j, &ne.p));
      if (!a || ne.p)
        continue;
      Error re;
      auto role = atspi_accessible_get_role(a.get(), &re.p);
      if (re.p || (role != ATSPI_ROLE_FRAME && role != ATSPI_ROLE_DIALOG &&
                   role != ATSPI_ROLE_WINDOW))
        continue;
      Error te;
      auto title = string_record(atspi_accessible_get_name(a.get(), &te.p), te)
                       .get("value")
                       .str();
      out.push_back({std::to_string(i) + ":" + std::to_string(j), appname,
                     title, std::move(a)});
    }
  }
  return out;
}
J style(GHashTable *attrs) {
  J out = J::object();
  if (!attrs)
    return out;
  for (auto pair :
       {std::pair<const char *, const char *>{"family-name", "font_family"},
        {"size", "font_size"},
        {"weight", "font_weight"},
        {"fg-color", "foreground"},
        {"bg-color", "background"},
        {"language", "language"}}) {
    auto v = static_cast<const char *>(g_hash_table_lookup(attrs, pair.first));
    if (!v)
      continue;
    std::string dest = pair.second;
    if (dest == "foreground" || dest == "background") {
      std::string numbers = v;
      for (char &c : numbers)
        if (c < '0' || c > '9')
          c = ' ';
      std::istringstream ss(numbers);
      int r, g, b, extra;
      if ((ss >> r >> g >> b) && !(ss >> extra) && r <= 255 && g <= 255 &&
          b <= 255) {
        char color[8];
        std::snprintf(color, sizeof(color), "#%02x%02x%02x", r, g, b);
        out.set(dest, color);
      }
    } else if (dest == "font_size" || dest == "font_weight") {
      char *end = nullptr;
      double number = std::strtod(v, &end);
      if (end != v && !*end && std::isfinite(number)) {
        out.set(dest, number);
        if (dest == "font_size")
          out.set("font_size_unit", "pt");
      }
    } else
      out.set(dest, bounded(v));
  }
  auto italic = static_cast<const char *>(g_hash_table_lookup(attrs, "style"));
  if (italic)
    out.set("italic", std::strcmp(italic, "italic") == 0);
  return out;
}
struct Reader {
  Rect viewport;
  AtspiCoordType coords;
  bool include_hidden;
  int width, height;
  J native = J::object();
  std::set<std::string> warnings;
  std::map<AtspiAccessible *, std::string> ids;
  std::set<AtspiAccessible *> seen;
  std::set<std::string> captured;
  std::vector<GPtr<AtspiAccessible>> refs;
  std::chrono::steady_clock::time_point start =
      std::chrono::steady_clock::now();
  Reader(Rect v, AtspiCoordType c, bool h, int w, int height_value)
      : viewport(v), coords(c), include_hidden(h), width(w),
        height(height_value) {}
  bool expired() {
    return seen.size() >= 5000 ||
           std::chrono::steady_clock::now() - start > std::chrono::seconds(20);
  }
  std::string id(AtspiAccessible *a) {
    auto it = ids.find(a);
    if (it != ids.end())
      return it->second;
    auto key = "n" + std::to_string(ids.size() + 1);
    ids[a] = key;
    refs.push_back(own(static_cast<AtspiAccessible *>(g_object_ref(a))));
    return key;
  }
  Rect mapped(const Rect &b) {
    return {(b[0] - viewport[0]) * width / viewport[2],
            (b[1] - viewport[1]) * height / viewport[3],
            b[2] * width / viewport[2], b[3] * height / viewport[3]};
  }
  J line(AtspiText *text, int begin, int end, const J &format) {
    Error te;
    J content = string_record(atspi_text_get_text(text, begin, end, &te.p), te);
    if (te.p)
      throw std::runtime_error("text");
    Error re;
    AtspiRect *r =
        atspi_text_get_range_extents(text, begin, end, coords, &re.p);
    Rect b = rect(r);
    g_free(r);
    if (re.p || !intersects(b, viewport))
      throw std::runtime_error("text bounds");
    J rectangles = J::array();
    rectangles.add(rectjson(mapped(b)));
    return J::object()
        .set("content", content.get("value"))
        .set("rectangles", rectangles)
        .set("style", format)
        .set("runs", J::array());
  }
  J text(AtspiAccessible *a) {
    auto iface = own(atspi_accessible_get_text_iface(a));
    J lines = J::array();
    if (!iface)
      return obs("not_supported").set("lines", lines);
    Error e;
    GArray *ranges = atspi_text_get_bounded_ranges(
        iface.get(), int(viewport[0]), int(viewport[1]), int(viewport[2]),
        int(viewport[3]), coords, ATSPI_TEXT_CLIP_BOTH, ATSPI_TEXT_CLIP_BOTH,
        &e.p);
    J result = obs(e.p ? "error" : "value")
                   .set("lines", lines)
                   .set("selections", obs("not_captured"));
    try {
      if (!e.p && ranges) {
        if (ranges->len > 2000) {
          result.set("status", "truncated");
          warnings.insert("Visible text truncated at 2000 ranges");
        }
        for (guint i = 0; i < std::min(ranges->len, 2000u); i++) {
          // libatspi's D-Bus demarshaller stores these structs inline, despite
          // older API prose describing a list of pointers.
          const auto &range = g_array_index(ranges, AtspiTextRange, i);
          int begin = range.start_offset, end = range.end_offset;
          if (begin < 0 || end < begin)
            throw std::runtime_error("text offsets");
          if (end - begin > 65536) {
            end = begin + 65536;
            result.set("status", "truncated");
            warnings.insert("Visible text truncated at 65536 characters");
          }
          J runs = J::array();
          bool hidden = false;
          int offset = begin;
          int run_count = 0;
          while (offset < end && run_count++ < 2048) {
            if (expired())
              throw std::runtime_error("text time limit");
            Error ae;
            int lo = offset, hi = offset;
            GHashTable *attrs = atspi_text_get_attribute_run(
                iface.get(), offset, TRUE, &lo, &hi, &ae.p);
            // If formatting is unreadable, do not emit potentially hidden text.
            if (ae.p || !attrs) {
              if (attrs)
                g_hash_table_unref(attrs);
              throw std::runtime_error("text attributes");
            }
            for (auto name : {"invisible", "hidden"}) {
              auto v =
                  static_cast<const char *>(g_hash_table_lookup(attrs, name));
              if (v &&
                  (std::strcmp(v, "true") == 0 || std::strcmp(v, "1") == 0))
                hidden = true;
            }
            J format = style(attrs);
            g_hash_table_unref(attrs);
            if (hidden)
              break;
            hi = std::min(end, hi);
            if (hi <= offset)
              throw std::runtime_error("text run bounds");
            runs.add(line(iface.get(), offset, hi, format));
            offset = hi;
          }
          if (hidden) {
            warnings.insert("Hidden formatted text redacted");
            continue;
          }
          if (offset < end) {
            result.set("status", "truncated");
            warnings.insert("Visible text formatting truncated at 2048 runs");
            end = offset;
          }
          if (end > begin)
            lines.add(
                line(iface.get(), begin, end, J::object()).set("runs", runs));
        }
      }
    } catch (const std::exception &) {
      result = obs("error").set("lines", J::array());
    }
    if (ranges) {
      for (guint i = 0; i < ranges->len; i++) {
        g_free(g_array_index(ranges, AtspiTextRange, i).content);
      }
      g_array_free(ranges, TRUE);
    }
    return result;
  }
  J node(AtspiAccessible *a, int depth = 0) {
    if (expired() || depth > 64) {
      warnings.insert(
          "AT-SPI traversal truncated by count, depth, or time limit");
      return J();
    }
    if (!seen.insert(a).second)
      return J();
    std::string key = id(a);
    captured.insert(key);
    J raw = J::object();
    native.set(key, raw);
    Error role_error;
    auto role = atspi_accessible_get_role(a, &role_error.p);
    Error rn;
    J role_record = string_record(atspi_accessible_get_role_name(a, &rn.p), rn);
    raw.set("role", role_record);
    bool protected_content =
        role_error.p || rn.p || role == ATSPI_ROLE_PASSWORD_TEXT ||
        role == ATSPI_ROLE_INVALID || role == ATSPI_ROLE_UNKNOWN;
    auto state = own(atspi_accessible_get_state_set(a));
    auto has = [&](AtspiStateType s) {
      return state && atspi_state_set_contains(state.get(), s);
    };
    J states = J::object().set("protected", protected_content);
    bool offscreen = !has(ATSPI_STATE_VISIBLE) || !has(ATSPI_STATE_SHOWING);
    states.set("offscreen", offscreen);
    if (state) {
      J all = J::array();
      GArray *arr = atspi_state_set_get_states(state.get());
      if (arr) {
        for (guint i = 0; i < arr->len; i++)
          all.add(int(g_array_index(arr, AtspiStateType, i)));
        g_array_free(arr, TRUE);
      }
      raw.set("states", value(all));
      for (auto pair : {std::pair<AtspiStateType, const char *>{
                            ATSPI_STATE_ENABLED, "enabled"},
                        {ATSPI_STATE_FOCUSED, "focused"},
                        {ATSPI_STATE_FOCUSABLE, "focusable"},
                        {ATSPI_STATE_SELECTED, "selected"},
                        {ATSPI_STATE_EDITABLE, "editable"}})
        states.set(pair.second, has(pair.first));
      if (role == ATSPI_ROLE_CHECK_BOX || role == ATSPI_ROLE_RADIO_BUTTON ||
          role == ATSPI_ROLE_TOGGLE_BUTTON)
        states.set("checked", has(ATSPI_STATE_INDETERMINATE) ? "mixed"
                              : has(ATSPI_STATE_CHECKED)     ? "checked"
                                                             : "unchecked");
      if (has(ATSPI_STATE_EXPANDABLE))
        states.set("expansion",
                   has(ATSPI_STATE_EXPANDED) ? "expanded" : "collapsed");
    } else
      raw.set("states", obs("error"));
    J bounds_record;
    Rect b = extents(a, coords, &bounds_record);
    raw.set("bounds", bounds_record);
    bool redacted =
        protected_content ||
        ((offscreen || !intersects(b, viewport)) && !include_hidden);
    J children = J::array(), relations = J::object();
    J out = J::object()
                .set("id", key)
                .set("role", role_name(role_record.get("value").str()))
                .set("bounds", rectjson(mapped(b)))
                .set("states", states)
                .set("relationships", relations)
                .set("children", children)
                .set("native_ref", key)
                .set("text", obs(redacted ? "redacted" : "not_supported")
                                 .set("lines", J::array()))
                .set("value", obs(redacted ? "redacted" : "not_captured"));
    if (bounds_record.get("status").str() != "value" || b[2] == 0 || b[3] == 0)
      out.set(
          "field_status",
          J::object().set("bounds", bounds_record.get("status").str() == "error"
                                        ? obs("error")
                                        : obs("not_captured")));
    if (!redacted) {
      for (auto pair :
           {std::pair<char *(*)(AtspiAccessible *, GError **), const char *>{
                atspi_accessible_get_name, "label"},
            {atspi_accessible_get_description, "description"}}) {
        Error e;
        J record = string_record(pair.first(a, &e.p), e);
        raw.set(pair.second, record);
        if (record.get("status").str() == "value")
          out.set(pair.second, record.get("value"));
      }
      Error app_error;
      auto app = own(atspi_accessible_get_application(a, &app_error.p));
      if (app && !app_error.p) {
        Error te;
        J toolkit = string_record(
            atspi_accessible_get_toolkit_name(app.get(), &te.p), te);
        out.set("hints", J::object().set("toolkit", toolkit.get("value")));
      }
      Error ae;
      GHashTable *attrs = atspi_accessible_get_attributes(a, &ae.p);
      J allowed = J::object();
      if (attrs) {
        for (auto name :
             {"level", "setsize", "posinset", "placeholder-text",
              "roledescription", "live", "atomic", "relevant", "invalid"}) {
          auto v = static_cast<const char *>(g_hash_table_lookup(attrs, name));
          if (v)
            allowed.set(name, bounded(v));
        }
        g_hash_table_unref(attrs);
      }
      raw.set("attributes", ae.p ? ae.record() : value(allowed));
      if (include_hidden) {
        auto vi = own(atspi_accessible_get_value_iface(a));
        if (vi) {
          Error ve;
          double number = atspi_value_get_current_value(vi.get(), &ve.p);
          J record = ve.p                    ? ve.record()
                     : std::isfinite(number) ? value(number)
                                             : obs("error");
          raw.set("value", record);
          out.set("value", record);
        }
      }
      out.set("text", text(a));
      Error re;
      GArray *rels = atspi_accessible_get_relation_set(a, &re.p);
      if (rels) {
        for (guint i = 0; i < rels->len; i++) {
          auto relation = own(g_array_index(rels, AtspiRelation *, i));
          if (!relation || re.p)
            continue;
          auto type = atspi_relation_get_relation_type(relation.get());
          GEnumClass *klass =
              G_ENUM_CLASS(g_type_class_ref(ATSPI_TYPE_RELATION_TYPE));
          auto ev = g_enum_get_value(klass, int(type));
          std::string name = normalized(ev ? ev->value_nick : "unknown");
          g_type_class_unref(klass);
          J targets = J::array();
          for (int j = 0;
               j < std::min(atspi_relation_get_n_targets(relation.get()), 256);
               j++) {
            auto target = own(atspi_relation_get_target(relation.get(), j));
            if (target && ids.size() < 20000)
              targets.add(id(target.get()));
            else if (target)
              warnings.insert(
                  "Relationship targets truncated at 20000 identifiers");
          }
          relations.set(name, targets);
        }
        g_array_free(rels, TRUE);
      }
      raw.set("relationships_status", re.p ? re.record() : obs("value"));
    }
    if (!protected_content) {
      Error ce;
      int n = atspi_accessible_get_child_count(a, &ce.p);
      raw.set("children_status", ce.p ? ce.record() : value(n));
      if (!ce.p) {
        if (n > 5000)
          warnings.insert("Child enumeration truncated at 5000");
        for (int i = 0; i < std::min(n, 5000); i++) {
          if (expired())
            break;
          Error e;
          auto child = own(atspi_accessible_get_child_at_index(a, i, &e.p));
          if (child && !e.p) {
            J c = node(child.get(), depth + 1);
            if (c.p)
              children.add(c);
          }
        }
      }
    }
    return out;
  }
  void prune(J n) {
    J rel = n.get("relationships");
    json_object_object_foreach(rel.p.get(), key, arr) {
      J kept = J::array();
      for (size_t i = 0; i < json_object_array_length(arr); i++) {
        const char *s =
            json_object_get_string(json_object_array_get_idx(arr, i));
        bool present = s && captured.count(s);
        if (present)
          kept.add(s);
      }
      json_object_object_add(rel.p.get(), key, json_object_get(kept.p.get()));
    }
    auto children = n.get("children");
    for (size_t i = 0; i < json_object_array_length(children.p.get()); i++)
      prune(J(json_object_get(json_object_array_get_idx(children.p.get(), i))));
  }
};
struct Image {
  int width = 0, height = 0;
  std::vector<unsigned char> rgb;
};
void dimensions(int w, int h) {
  if (w <= 0 || h <= 0 || w > 32768 || h > 32768 ||
      uint64_t(w) * h > 64 * 1024 * 1024)
    throw std::runtime_error("Image dimensions exceed capture limits");
}
Image load_image(const std::string &path) {
  png_image p{};
  p.version = PNG_IMAGE_VERSION;
  if (!png_image_begin_read_from_file(&p, path.c_str()))
    throw std::runtime_error("Cannot read supplied PNG");
  try {
    dimensions(int(p.width), int(p.height));
    p.format = PNG_FORMAT_RGB;
    Image out{int(p.width), int(p.height),
              std::vector<unsigned char>(PNG_IMAGE_SIZE(p))};
    if (!png_image_finish_read(&p, nullptr, out.rgb.data(), 0, nullptr))
      throw std::runtime_error("Cannot decode supplied PNG");
    png_image_free(&p);
    return out;
  } catch (...) {
    png_image_free(&p);
    throw;
  }
}
int x_error(Display *, XErrorEvent *) { return 0; }
Image screen_image(const Rect &b) {
  dimensions(int(b[2]), int(b[3]));
  auto display = std::unique_ptr<Display, int (*)(Display *)>(
      XOpenDisplay(nullptr), XCloseDisplay);
  if (!display)
    throw std::runtime_error("Cannot open X11 DISPLAY");
  int screen = DefaultScreen(display.get());
  if (DefaultVisual(display.get(), screen)->c_class != TrueColor)
    throw std::runtime_error("X11 visual must be TrueColor");
  if (b[0] < 0 || b[1] < 0 ||
      b[0] + b[2] > DisplayWidth(display.get(), screen) ||
      b[1] + b[3] > DisplayHeight(display.get(), screen))
    throw std::runtime_error(
        "Window extends outside X11 screen; move it fully onscreen");
  XSetErrorHandler(x_error);
  auto pixels = std::unique_ptr<XImage, int (*)(XImage *)>(
      XGetImage(display.get(), RootWindow(display.get(), screen), int(b[0]),
                int(b[1]), unsigned(b[2]), unsigned(b[3]), AllPlanes, ZPixmap),
      +[](XImage *p) { return XDestroyImage(p); });
  if (!pixels)
    throw std::runtime_error("X11 screenshot failed");
  Image out{int(b[2]), int(b[3]),
            std::vector<unsigned char>(size_t(b[2]) * size_t(b[3]) * 3)};
  auto channel = [](unsigned long pixel, unsigned long mask) {
    if (!mask)
      return 0ul;
    while (!(mask & 1)) {
      mask >>= 1;
      pixel >>= 1;
    }
    return (pixel & mask) * 255 / mask;
  };
  for (int y = 0; y < out.height; y++)
    for (int x = 0; x < out.width; x++) {
      auto pixel = XGetPixel(pixels.get(), x, y);
      size_t i = (size_t(y) * out.width + x) * 3;
      out.rgb[i] = channel(pixel, pixels->red_mask);
      out.rgb[i + 1] = channel(pixel, pixels->green_mask);
      out.rgb[i + 2] = channel(pixel, pixels->blue_mask);
    }
  return out;
}
void be32(std::vector<unsigned char> &v, uint32_t n) {
  for (int shift : {24, 16, 8, 0})
    v.push_back((n >> shift) & 255);
}
std::vector<unsigned char> semantic_png(const Image &image, const J &snapshot) {
  png_image p{};
  p.version = PNG_IMAGE_VERSION;
  p.width = image.width;
  p.height = image.height;
  p.format = PNG_FORMAT_RGB;
  png_alloc_size_t size = 0;
  if (!png_image_write_to_memory(&p, nullptr, &size, 0, image.rgb.data(), 0,
                                 nullptr))
    throw std::runtime_error("PNG sizing failed");
  std::vector<unsigned char> png(size);
  if (!png_image_write_to_memory(&p, png.data(), &size, 0, image.rgb.data(), 0,
                                 nullptr))
    throw std::runtime_error("PNG encoding failed");
  png.resize(size);
  png_image_free(&p);
  std::string json = snapshot.dump();
  if (json.size() > 64 * 1024 * 1024)
    throw std::runtime_error("Snapshot exceeds 64 MiB");
  uLongf length = compressBound(json.size());
  std::vector<unsigned char> compressed(length);
  if (compress2(compressed.data(), &length,
                reinterpret_cast<const Bytef *>(json.data()), json.size(),
                9) != Z_OK)
    throw std::runtime_error("JSON compression failed");
  compressed.resize(length);
  std::vector<unsigned char> payload = {'S', 'V', 'G', 'S', 'H', 'O',
                                        'T', 0,   1,   1,   1,   0};
  be32(payload, uint32_t(json.size()));
  payload.insert(payload.end(), compressed.begin(), compressed.end());
  if (payload.size() > 16 * 1024 * 1024)
    throw std::runtime_error("Metadata exceeds 16 MiB");
  std::vector<unsigned char> chunk;
  be32(chunk, uint32_t(payload.size()));
  chunk.insert(chunk.end(), {'s', 'e', 'M', 'A'});
  chunk.insert(chunk.end(), payload.begin(), payload.end());
  be32(chunk, uint32_t(crc32(0, chunk.data() + 4, uInt(chunk.size() - 4))));
  png.insert(png.end() - 12, chunk.begin(), chunk.end());
  return png;
}
void atomic_write(const std::string &path,
                  const std::vector<unsigned char> &data) {
  std::string pattern = path + ".XXXXXX";
  std::vector<char> temp(pattern.begin(), pattern.end());
  temp.push_back(0);
  int fd = mkstemp(temp.data());
  if (fd < 0)
    throw std::runtime_error("Cannot create capture output");
  bool success = true;
  size_t offset = 0;
  while (offset < data.size()) {
    ssize_t n = write(fd, data.data() + offset, data.size() - offset);
    if (n < 0 && errno == EINTR)
      continue;
    if (n <= 0) {
      success = false;
      break;
    }
    offset += size_t(n);
  }
  if (close(fd) != 0)
    success = false;
  if (!success || rename(temp.data(), path.c_str()) != 0) {
    unlink(temp.data());
    throw std::runtime_error("Cannot write capture output");
  }
}
int main(int argc, char **argv) {
  try {
    std::string window, out, json, bitmap;
    bool foreground = false, hidden = false, stdout_png = false, list = false;
    int delay = 0;
    Rect supplied{};
    bool has_bounds = false;
    for (int i = 1; i < argc; i++) {
      std::string arg = argv[i];
      auto next = [&]() {
        if (++i >= argc)
          throw std::runtime_error("Incomplete option " + arg);
        return std::string(argv[i]);
      };
      if (arg == "--version") {
        std::cout << "svgshot-capture-linux " << SVGSHOT_COMMIT << "\n";
        return 0;
      }
      if (arg == "--help") {
        std::cout
            << "svgshot-capture-linux [--out capture.png | --stdout] [--window "
               "ID | --foreground] [--delay SECONDS] [--json capture.json] "
               "[--include-hidden-content]\n  --list-windows   List "
               "capture-local AT-SPI window IDs\n  --version        Report "
               "source commit\n  --bitmap PNG --window-bounds X Y W H   "
               "Explicit Wayland bitmap pairing\n";
        return 0;
      }
      if (arg == "--window")
        window = next();
      else if (arg == "--out")
        out = next();
      else if (arg == "--json")
        json = next();
      else if (arg == "--bitmap")
        bitmap = next();
      else if (arg == "--delay")
        delay = std::stoi(next());
      else if (arg == "--window-bounds") {
        for (auto &n : supplied) {
          n = std::stod(next());
          if (!std::isfinite(n) || n != std::floor(n) || std::abs(n) > 1000000)
            throw std::runtime_error(
                "Bounds must be finite integer AT-SPI coordinates");
        }
        has_bounds = true;
      } else if (arg == "--foreground")
        foreground = true;
      else if (arg == "--stdout")
        stdout_png = true;
      else if (arg == "--include-hidden-content")
        hidden = true;
      else if (arg == "--list-windows")
        list = true;
      else
        throw std::runtime_error("Unknown option " + arg);
    }
    if (delay < 0 || delay > 60)
      throw std::runtime_error("Delay must be 0–60 seconds");
    if (!window.empty() && foreground)
      throw std::runtime_error("Choose --window or --foreground");
    if (!list) {
      if (out.empty() == !stdout_png)
        throw std::runtime_error("Choose --out PNG or --stdout");
      if (!json.empty() && (json == out || json == bitmap))
        throw std::runtime_error("JSON must not overwrite a PNG");
      if (bitmap.empty() != !has_bounds)
        throw std::runtime_error("--bitmap requires --window-bounds");
    }
    if (atspi_init() != 0)
      throw std::runtime_error("Cannot initialize AT-SPI");
    atspi_set_timeout(1000, 5000);
    if (delay)
      std::this_thread::sleep_for(std::chrono::seconds(delay));
    auto choices = windows();
    if (list) {
      J a = J::array();
      for (auto &w : choices)
        a.add(J::object()
                  .set("id", w.id)
                  .set("application", w.application)
                  .set("title", w.title));
      std::cout << a.dump() << "\n";
      return 0;
    }
    if (window.empty() && !foreground) {
      if (!isatty(STDIN_FILENO))
        throw std::runtime_error(
            "Use --list-windows then --window ID, or --foreground");
      for (auto &w : choices)
        std::cerr << w.id << ": " << w.application << " — " << w.title << "\n";
      std::cerr << "Window ID: " << std::flush;
      std::getline(std::cin, window);
    }
    AtspiAccessible *target = nullptr;
    for (auto &w : choices) {
      auto state = own(atspi_accessible_get_state_set(w.node.get()));
      bool active = foreground && state &&
                    atspi_state_set_contains(state.get(), ATSPI_STATE_ACTIVE);
      if (w.id == window || active) {
        if (target)
          throw std::runtime_error("Window target is ambiguous");
        target = w.node.get();
      }
    }
    if (!target)
      throw std::runtime_error("Window target missing; refresh --list-windows");
    bool wayland = std::getenv("WAYLAND_DISPLAY") != nullptr;
    if (wayland && bitmap.empty())
      throw std::runtime_error(
          "Automatic Wayland association is not implemented; use --bitmap and "
          "--window-bounds with --window ID");
    auto coords = wayland ? ATSPI_COORD_TYPE_WINDOW : ATSPI_COORD_TYPE_SCREEN;
    J before_record;
    Rect before = extents(target, coords, &before_record);
    if (before_record.get("status").str() != "value")
      throw std::runtime_error("Cannot read window bounds");
    Rect bounds = bitmap.empty() ? before : supplied;
    if (bounds[2] <= 0 || bounds[3] <= 0)
      throw std::runtime_error("Invalid capture bounds");
    Image image = bitmap.empty() ? screen_image(bounds) : load_image(bitmap);
    Reader reader{bounds, coords, hidden, image.width, image.height};
    reader.warnings.insert(
        bitmap.empty()
            ? "X11 capture records visible screen pixels; overlapping windows "
              "may obscure the target"
            : "Bitmap/window association supplied explicitly by the user");
    J root = reader.node(target);
    reader.prune(root);
    J after_record;
    Rect after = extents(target, coords, &after_record);
    if (after_record.get("status").str() != "value" || before != after)
      throw std::runtime_error("Window moved or resized during capture");
    J sizes = J::array();
    sizes.add(image.width);
    sizes.add(image.height);
    J transform = J::array();
    for (double x :
         {image.width / bounds[2], 0.0, 0.0, image.height / bounds[3],
          -bounds[0] * image.width / bounds[2],
          -bounds[1] * image.height / bounds[3]})
      transform.add(x);
    J warnings = J::array();
    for (auto &w : reader.warnings)
      warnings.add(w);
    J snapshot =
        J::object()
            .set("format", "svgshot.capture")
            .set("version", 3)
            .set("source", J::object()
                               .set("platform", "linux")
                               .set("provider", "linux-atspi")
                               .set("commit", SVGSHOT_COMMIT))
            .set("image", J::object()
                              .set("size", sizes)
                              .set("coordinate_space", "image-pixels")
                              .set("source_bounds", rectjson(bounds))
                              .set("source_to_image", transform))
            .set("root", root)
            .set("capture_policy", J::object()
                                       .set("include_hidden_content", hidden)
                                       .set("password_content", "redacted")
                                       .set("actions_invoked", false))
            .set("warnings", warnings)
            .set("native", J::object()
                               .set("provider", "linux-atspi")
                               .set("nodes", reader.native));
    auto png = semantic_png(image, snapshot);
    if (stdout_png) {
      std::cout.write(reinterpret_cast<const char *>(png.data()),
                      std::streamsize(png.size()));
      if (!std::cout)
        throw std::runtime_error("Cannot write PNG stdout");
    } else
      atomic_write(out, png);
    if (!json.empty()) {
      auto text = snapshot.dump() + "\n";
      atomic_write(json, std::vector<unsigned char>(text.begin(), text.end()));
    }
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "svgshot capture: " << e.what() << "\n";
    return 1;
  }
}
