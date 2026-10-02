#pragma once

// MSAA providers in older DPI-virtualized applications can mix logical screen
// rectangles with physical window origins. Normalize against the owning HWND,
// never against a single scale factor for the entire captured window.
struct MsaaGeometry
{
    HWND hwnd = nullptr;
    double scaleX = 1, scaleY = 1, dx = 0, dy = 0;
    bool corrected = false;
    double rowScaleX = 1, rowScaleY = 1;
    POINT rowOrigin{};
    RECT apply(RECT r) const
    {
        return {static_cast<LONG>(std::lround(r.left * scaleX + dx)),
                static_cast<LONG>(std::lround(r.top * scaleY + dy)),
                static_cast<LONG>(std::lround(r.right * scaleX + dx)),
                static_cast<LONG>(std::lround(r.bottom * scaleY + dy))};
    }
};
inline bool near_rect(RECT a, RECT b)
{
    return std::abs(a.left-b.left) <= 2 && std::abs(a.top-b.top) <= 2 &&
           std::abs(a.right-b.right) <= 2 && std::abs(a.bottom-b.bottom) <= 2;
}
inline RECT client_screen_rect(HWND hwnd)
{
    RECT r{};
    if (GetClientRect(hwnd, &r))
        MapWindowPoints(hwnd, nullptr, reinterpret_cast<POINT *>(&r), 2);
    return r;
}
inline MsaaGeometry msaa_geometry(IAccessible *object, RECT self, MsaaGeometry inherited)
{
    HWND hwnd = nullptr;
    if (FAILED(WindowFromAccessibleObject(object, &hwnd)) || !hwnd)
        return inherited;
    MsaaGeometry result = inherited.hwnd == hwnd ? inherited : MsaaGeometry{};
    result.hwnd = hwnd;
    RECT physical = client_screen_rect(hwnd);
    auto previous = SetThreadDpiAwarenessContext(GetWindowDpiAwarenessContext(hwnd));
    RECT logical = client_screen_rect(hwnd);
    if (previous) SetThreadDpiAwarenessContext(previous);
    if (near_rect(self, physical))
    {
        result = MsaaGeometry{hwnd};
        wchar_t name[64]{};
        GetClassNameW(hwnd, name, ARRAYSIZE(name));
        // Verify the report header actually exhibits logical coordinates before
        // correcting the common-control proxy's mixed-origin simple rows.
        DWORD_PTR headerResult = 0;
        if (std::wstring(name) == WC_LISTVIEWW &&
            SendMessageTimeoutW(hwnd, LVM_GETHEADER, 0, 0, SMTO_ABORTIFHUNG, 200, &headerResult))
        {
            auto header = reinterpret_cast<HWND>(headerResult);
            winrt::com_ptr<IAccessible> accessible;
            if (header && SUCCEEDED(AccessibleObjectFromWindow(header, static_cast<DWORD>(OBJID_CLIENT),
                                                               IID_IAccessible, accessible.put_void())))
            {
                VARIANT child{}; child.vt = VT_I4; child.lVal = CHILDID_SELF;
                LONG x=0, y=0, w=0, h=0;
                if (SUCCEEDED(accessible->accLocation(&x, &y, &w, &h, child)))
                {
                    auto headerGeometry = msaa_geometry(accessible.get(), {x,y,x+w,y+h}, {});
                    if (headerGeometry.corrected)
                    {
                        result.rowScaleX = headerGeometry.scaleX;
                        result.rowScaleY = headerGeometry.scaleY;
                        result.rowOrigin = {physical.left, physical.top};
                    }
                }
            }
        }
        return result;
    }
    if (!near_rect(logical, physical) && near_rect(self, logical) &&
        logical.right > logical.left && logical.bottom > logical.top)
    {
        result.scaleX = double(physical.right-physical.left)/(logical.right-logical.left);
        result.scaleY = double(physical.bottom-physical.top)/(logical.bottom-logical.top);
        result.dx = physical.left-logical.left*result.scaleX;
        result.dy = physical.top-logical.top*result.scaleY;
        result.corrected = true;
    }
    return result;
}

inline bool msaa_geometry_self_test()
{
    // 150% scaling, including a monitor to the left of the primary display.
    MsaaGeometry logical;
    logical.scaleX = logical.scaleY = 1.5;
    if (!near_rect(logical.apply({-600, 100, -400, 124}), {-900,150,-600,186})) return false;
    MsaaGeometry mixed;
    mixed.scaleX = mixed.scaleY = 1.5;
    mixed.dx = -647*.5;
    mixed.dy = -261*.5;
    if (!near_rect(mixed.apply({647,285,1151,304}), {647,297,1403,326})) return false;
    MsaaGeometry physical;
    return near_rect(physical.apply({647,261,1589,297}), {647,261,1589,297});
}
