#!/bin/bash
# GitHub Setup Script for ops-automation-engine
# This script automates the GitHub repository setup and secret configuration

set -e

echo "================================"
echo "GitHub Setup for ops-automation-engine"
echo "================================"
echo ""

# Check if git is available
if ! command -v git &> /dev/null; then
    echo "❌ Git is not installed. Please install Git first."
    exit 1
fi

# Get GitHub username
read -p "Enter your GitHub username: " GITHUB_USERNAME
read -p "Enter repository name (default: ops-automation-engine): " REPO_NAME
REPO_NAME=${REPO_NAME:-ops-automation-engine}

REPO_URL="https://github.com/${GITHUB_USERNAME}/${REPO_NAME}.git"

echo ""
echo "Repository URL: $REPO_URL"
echo ""

# Check if remote already exists
if git remote get-url origin &> /dev/null; then
    echo "⚠️  Remote 'origin' already configured as: $(git remote get-url origin)"
    read -p "Do you want to update it to $REPO_URL? (y/n): " UPDATE_REMOTE
    if [[ $UPDATE_REMOTE == "y" ]]; then
        git remote remove origin
        git remote add origin "$REPO_URL"
        echo "✅ Remote updated"
    fi
else
    git remote add origin "$REPO_URL"
    echo "✅ Remote added"
fi

# Push to main
echo ""
echo "Pushing code to GitHub main branch..."
git push -u origin main --force
echo "✅ Code pushed to main branch"

echo ""
echo "================================"
echo "Next: Add Secrets to GitHub"
echo "================================"
echo ""
echo "1. Go to: https://github.com/${GITHUB_USERNAME}/${REPO_NAME}/settings/secrets/actions"
echo ""
echo "2. Create secret: DOCKER_USERNAME"
echo "   Value: Your Docker Hub username"
echo ""
echo "3. Create secret: DOCKER_PASSWORD"
echo "   Value: Your Docker Hub Personal Access Token"
echo "   (Get it at: https://hub.docker.com/settings/security)"
echo ""
echo "4. After adding secrets, push an empty commit to trigger the workflow:"
echo "   git commit --allow-empty -m 'Trigger CI/CD pipeline'"
echo "   git push origin main"
echo ""
echo "5. Watch the build at:"
echo "   https://github.com/${GITHUB_USERNAME}/${REPO_NAME}/actions"
echo ""
