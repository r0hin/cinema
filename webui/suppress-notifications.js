// Register before Angular bootstraps, so even startup notifications are filtered.
angular.module("stremio").config([
  "$provide",
  function ($provide) {
    $provide.decorator("toasts", [
      "$delegate",
      function (toasts) {
        var suppressed = ["ERR_CASTING_UPDATE_TITLE", "TRAKT_EXPIRED"];
        ["error", "warning", "info", "success"].forEach(function (level) {
          var original = toasts[level];
          toasts[level] = function (title) {
            if (suppressed.indexOf(title) !== -1) return;
            return original.apply(this, arguments);
          };
        });
        return toasts;
      },
    ]);
  },
]);
