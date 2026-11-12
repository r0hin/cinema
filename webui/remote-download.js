(function () {
  "use strict";
  angular.module("stremio").directive("cinemaDownload", ["$http", "$timeout", "storage", function ($http, $timeout, storage) {
    function english(sub) { return sub && /^(en|eng|english)([-_].*)?$/i.test(sub.lang || ""); }
    function chooseSubtitle(subs) {
      if (!subs) return null;
      var current = subs.current && subs.current();
      if (english(current) && current.url) return current;
      var all = (subs.all || []).filter(function (sub) { return english(sub) && sub.url; });
      return all.find(function (sub) { return subs.preferred && subs.preferred[sub.lang] === sub.id; }) || all[0] || null;
    }
    return {
      restrict: "A",
      scope: true,
      template: '<button type="button" class="cinema-download-button" ng-if="remoteEnabled" ng-click="saveRemote($event)" ng-disabled="remoteBusy || !selected.stream.stream.url" title="{{remoteMessage || \'Save video and English subtitles to compute-alt/Movies\'}}" aria-label="Save to compute-alt" ng-bind="remoteLabel"></button><span class="cinema-download-status" ng-if="remoteEnabled &amp;&amp; remoteMessage" role="status">{{remoteMessage}}</span>',
      link: function (scope) {
        var timer, destroyed = false, generation = 0;
        scope.remoteLabel = "Save to compute-alt";
        $http.get("downloads/config").then(function (result) { scope.remoteEnabled = result.data.enabled; });
        function headers() { return { Authorization: "Bearer " + (storage.getJSON("authKey") || "") }; }
        function failed(error) {
          scope.remoteBusy = false;
          scope.remoteLabel = "Retry save";
          scope.remoteMessage = error.data && error.data.error || error.message || "Save failed. Try again.";
        }
        function poll(id, run) {
          $http.get("downloads/" + id, { headers: headers() }).then(function (result) {
            if (destroyed || run !== generation) return;
            var job = result.data;
            scope.remoteMessage = job.message;
            scope.remoteLabel = job.status === "complete" ? "Saved to compute-alt" : job.status === "failed" ? "Retry save" : job.status === "queued" ? "Queued…" : "Saving…";
            scope.remoteBusy = job.status !== "complete" && job.status !== "failed";
            if (scope.remoteBusy) timer = $timeout(function () { poll(id, run); }, 4000);
          }, function (error) { if (!destroyed && run === generation) failed(error); });
        }
        scope.saveRemote = function (event) {
          event.stopPropagation();
          if (scope.remoteBusy) return;
          var selected = scope.selected || {}, stream = selected.stream && selected.stream.stream;
          if (!stream || !stream.url) return;
          var subtitle = chooseSubtitle(selected.subtitles);
          if (!subtitle) return failed(new Error("No English subtitle file is available yet. Choose one in the subtitle menu, then retry."));
          var title = selected.item && selected.item.title(scope.info) || scope.info && scope.info.name || "Movie";
          var year = scope.info && String(scope.info.releaseInfo || scope.info.year || "").match(/\b(?:19|20)\d{2}\b/);
          if (year && title.indexOf(year[0]) === -1) title += " (" + year[0] + ")";
          var video = selected.video;
          if (video && video.season != null && (video.episode != null || video.number != null)) {
            title += " S" + ("0" + video.season).slice(-2) + "E" + ("0" + (video.episode || video.number)).slice(-2);
          }
          scope.remoteBusy = true;
          scope.remoteLabel = "Queuing…";
          scope.remoteMessage = "";
          var run = ++generation;
          $http.post("downloads", {
            title: title,
            videoUrl: stream.url,
            filename: selected.stream.behaviorHints && selected.stream.behaviorHints.filename || selected.stream.title || "",
            subtitleUrl: subtitle.url,
            subtitleDelay: selected.subtitles.current && selected.subtitles.current() === subtitle ? selected.subtitles.currentDelay() : 0,
          }, { headers: headers() }).then(function (result) {
            if (!destroyed && run === generation) poll(result.data.id, run);
          }, function (error) { if (!destroyed && run === generation) failed(error); });
        };
        scope.$watch("selected.stream", function () {
          generation++;
          $timeout.cancel(timer);
          scope.remoteBusy = false;
          scope.remoteMessage = "";
          scope.remoteLabel = "Save to compute-alt";
        });
        scope.$on("$destroy", function () { destroyed = true; $timeout.cancel(timer); });
      },
    };
  }]);
}());
