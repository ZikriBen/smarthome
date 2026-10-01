# Homarr Custom CSS

Use explicit Homarr item classes rather than broad selectors.

## Classes

### glass-widget

Standard widgets:

```text
Weather
Uptime
Server stats
Calendar
Speedtest
AdGuard
```

### glass-light

Application groups:

```text
Home Assistant
Beszel
Kuma
Gmail
YouTube
ChatGPT
```

### glass-strong

Text-heavy widgets such as:

```text
RSS / News
```

### theme-inner

Normalizes internal widget card colors.

Example:

```text
glass-widget theme-inner
```

### hide-mobile

Hide selected widgets on screens up to 700px.

Note: Homarr may still reserve grid space.
Prefer Responsive Layouts when hiding creates holes.

## Theme

Main color:

```text
#181B22
```

RGB:

```text
rgb(24, 27, 34)
```

Transparent version:

```text
rgba(24, 27, 34, 0.78)
```
