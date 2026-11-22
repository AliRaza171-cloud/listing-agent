// Jenkinsfile for Listing Agent (github.com/AliRaza171-cloud/listing-agent)
// Put this file in the repo root, next to README.md, and push it to GitHub.
//
// Runs on a Windows Jenkins agent. No Docker and no secrets are needed:
// every service is loaded with placeholder settings, and nothing connects
// to a real database, Redis or AI provider.
//
// One-time setup in Jenkins:
//   New Item > "listing-agent" > Pipeline
//   Pipeline: "Pipeline script from SCM" > Git
//     Repository URL: https://github.com/AliRaza171-cloud/listing-agent.git
//     Branch: */main      Script Path: Jenkinsfile
//   Python and Node must be on the PATH of the account Jenkins runs as.

pipeline {
  agent any

  environment {
    PYTHONUNBUFFERED        = '1'
    NEXT_TELEMETRY_DISABLED = '1'
    // Placeholder settings so every service can start up. Nothing connects to them.
    DATABASE_URL   = 'postgresql://ci:ci@localhost:5432/ci'
    INTERNAL_TOKEN = 'ci-internal-token'
    JWT_SECRET     = 'ci-jwt-secret'
  }

  options {
    timestamps()
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20'))
  }

  triggers {
    // Jenkins on a laptop can't receive GitHub webhooks without a public URL,
    // so it checks GitHub for new commits every 2 minutes instead.
    pollSCM('H/2 * * * *')
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    stage('Build & Check') {
      parallel {
        stage('Services') {
          stages {
            stage('Services: setup') {
              steps {
                bat 'python -m venv .venv'
                bat '.venv\\Scripts\\python -m pip install --upgrade pip'
                bat '.venv\\Scripts\\python -m pip install ./libs/common'
                powershell '''
                  $ErrorActionPreference = "Stop"
                  Get-ChildItem services -Filter requirements.txt -Recurse | ForEach-Object {
                    Write-Host "Installing $($_.FullName)"
                    & .venv/Scripts/python.exe -m pip install -r $_.FullName
                    if ($LASTEXITCODE -ne 0) { throw "pip install failed for $($_.FullName)" }
                  }
                '''
              }
            }
            stage('Services: check') {
              steps {
                // 1. every Python file compiles (catches syntax errors)
                bat '.venv\\Scripts\\python -m compileall -q libs services'
                // 2. every microservice loads (catches import errors, missing packages, bad settings)
                powershell '''
                  $ErrorActionPreference = "Stop"
                  $env:EVENTS_CONTRACT_PATH = "$env:WORKSPACE/contracts/events.json"
                  $py = "$env:WORKSPACE/.venv/Scripts/python.exe"
                  foreach ($s in "gateway","auth","catalog","ai","voice","store","publisher","billing","notification") {
                    Push-Location "services/$s"
                    & $py -c "import app.main"
                    if ($LASTEXITCODE -ne 0) { throw "Service '$s' failed to load" }
                    Write-Host "OK  $s"
                    Pop-Location
                  }
                '''
              }
            }
          }
        }

        stage('Web app') {
          stages {
            stage('Web: setup') {
              steps {
                dir('apps/web') {
                  bat 'npm ci'
                }
              }
            }
            stage('Web: type check') {
              steps {
                dir('apps/web') {
                  bat 'npm run typecheck'
                }
              }
            }
            stage('Web: build') {
              steps {
                dir('apps/web') {
                  bat 'npm run build'
                }
              }
            }
          }
        }
      }
    }
  }

  post {
    success {
      echo 'Listing Agent: all 9 services load and the web app builds.'
    }
    failure {
      echo 'Listing Agent: build failed. Open the red stage to see its log.'
    }
  }
}
