(define-module (conductor)
  #:use-module ((guix licenses) #:prefix license:)
  #:use-module (gnu packages)
  #:use-module (gnu packages python-xyz)
  #:use-module (gnu packages python-web)
  #:use-module (gnu packages check)
  #:use-module (gnu packages ssh)
  #:use-module (guix packages)
  #:use-module (guix download)
  #:use-module (guix build-system python)
  #:use-module (guix gexp)
  #:use-module (srfi srfi-1)
  #:use-module (srfi srfi-26))

(define %source-dir (dirname (dirname (current-filename))))

;; Backport of https://github.com/ronf/asyncssh/issues/476 until new release.
(define python-asyncssh-fixed
  (package-with-patches python-asyncssh '("contrib/python-asyncssh-fix-gssapi.patch")))

(package
  (name "conductor")
  (version "0.1")
  (source (local-file %source-dir #:recursive? #t))
  (build-system python-build-system)
  (propagated-inputs
   `(("python-asyncssh" ,python-asyncssh-fixed)
     ("python-multidict" ,python-multidict)
     ("python-parse" ,python-parse)
     ("python-furl" ,python-furl)))
  (native-inputs
   `(("python-pytest-asyncio" ,python-pytest-asyncio)
     ("python-pytest" ,python-pytest)
     ("python-pytest-cov" ,python-pytest-cov)
     ("python-aiohttp" ,python-aiohttp)))
  (home-page "https://github.com/leibniz-psychology/conductor")
  (synopsis #f)
  (description #f)
  (license #f))

