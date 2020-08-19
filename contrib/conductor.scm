(define-module (conductor)
  #:use-module ((guix licenses) #:prefix license:)
  #:use-module (gnu packages)
  #:use-module (gnu packages python-xyz)
  #:use-module (gnu packages python-web)
  #:use-module (gnu packages check)
  #:use-module (zpid packages asyncssh)
  #:use-module (guix packages)
  #:use-module (guix download)
  #:use-module (guix build-system python)
  #:use-module (guix gexp)
  #:use-module (srfi srfi-1)
  #:use-module (srfi srfi-26))

(define %source-dir (dirname (dirname (current-filename))))

(package
  (name "conductor")
  (version "0.1")
  (source (local-file %source-dir #:recursive? #t))
  (build-system python-build-system)
  (propagated-inputs
   `(("python-asyncssh" ,python-asyncssh)
     ("python-yarl" ,python-yarl)
     ("python-aionotify" ,python-aionotify)
     ("python-parse" ,python-parse)
     ("python-pytest" ,python-pytest)))
  (home-page "https://github.com/leibniz-psychology/conductor")
  (synopsis #f)
  (description #f)
  (license #f))

